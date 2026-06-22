import json
import os
import time
import uuid
import uvicorn
import openai
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, status, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel
from agents import (
    Agent, Runner, function_tool, set_tracing_disabled,
    GuardrailFunctionOutput, InputGuardrailTripwireTriggered,
    RunContextWrapper, AgentHooks, RunHooks, RunConfig, input_guardrail,
    output_guardrail, tool_input_guardrail,
    ToolGuardrailFunctionOutput, handoff, SQLiteSession,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.extensions import handoff_filters
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.tracing import (
    trace, add_trace_processor, TracingProcessor,
    AgentSpanData, FunctionSpanData, HandoffSpanData,
    GuardrailSpanData, GenerationSpanData, SpanError,
)
import database as db

load_dotenv(override=True)

SECRET_KEY = os.getenv("JWT_SECRET", "fallback-secret-change-in-production")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    user_id: Optional[str] = None


class UserOut(BaseModel):
    id: str
    email: str
    username: str
    role: str
    created_at: str


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str


class SupportContext(BaseModel):
    """Context shared across the support agent lifecycle."""
    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    account_tier: str = "standard"
    conversation_id: Optional[str] = None
    tools_called: int = 0
    orders_looked_up: int = 0
    tickets_checked: int = 0
    policies_checked: int = 0
    escalations_created: int = 0
    tasks: list[dict] = []
    tickets: list[dict] = []


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.get_user_by_id(user_id)
    if user is None:
        raise credentials_exception
    return user
# ── Tracing & Observability ──

class SupportTracingProcessor(TracingProcessor):
    """Stores traces and spans in memory for the observability endpoint."""

    def __init__(self, max_traces: int = 50, max_spans: int = 500):
        self.traces: list[dict] = []
        self.spans: list[dict] = []
        self.max_traces = max_traces
        self.max_spans = max_spans
        self._trace_map: dict[str, dict] = {}

    def on_trace_start(self, trace_obj) -> None:
        entry = {
            "trace_id": trace_obj.trace_id,
            "workflow_name": trace_obj.name,
            "group_id": trace_obj.group_id,
            "metadata": trace_obj.metadata,
            "event": "start",
            "timestamp": time.time(),
        }
        self.traces.append(entry)
        self._trace_map[trace_obj.trace_id] = entry
        if len(self.traces) > self.max_traces:
            self.traces.pop(0)

    def on_trace_end(self, trace_obj) -> None:
        entry = self._trace_map.get(trace_obj.trace_id)
        if entry:
            entry["event"] = "end"
            entry["ended_at"] = time.time()
            entry["duration_ms"] = round((entry["ended_at"] - entry["timestamp"]) * 1000, 2)

    def on_span_start(self, span) -> None:
        span_data = span.span_data
        entry = {
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "parent_id": span.parent_id,
            "span_type": type(span_data).__name__ if span_data else None,
            "event": "start",
            "timestamp": time.time(),
            "name": getattr(span_data, "name", None)
                     or (span_data.output if isinstance(span_data, AgentSpanData) and span_data.output else None)
                     or None,
            "input": self._serialize_for_display(span_data, "input"),
            "output": self._serialize_for_display(span_data, "output"),
            "error": None,
        }
        self.spans.append(entry)
        if len(self.spans) > self.max_spans:
            self.spans.pop(0)

    def on_span_end(self, span) -> None:
        for s in self.spans:
            if s["span_id"] == span.span_id:
                s["event"] = "end"
                s["ended_at"] = time.time()
                s["duration_ms"] = round((s["ended_at"] - s["timestamp"]) * 1000, 2)
                if span.error:
                    s["error"] = {"message": str(span.error.message), "code": span.error.code}
                # Update output from ended span data
                sd = span.span_data
                if sd:
                    output_val = self._serialize_for_display(sd, "output")
                    if output_val:
                        s["output"] = output_val
                break

    def force_flush(self) -> None:
        pass

    def shutdown(self) -> None:
        self.traces.clear()
        self.spans.clear()
        self._trace_map.clear()

    def _serialize_for_display(self, span_data, field: str):
        try:
            raw = getattr(span_data, field, None)
            if raw is None:
                return None
            if isinstance(raw, str):
                return raw[:500]
            if isinstance(raw, (list, dict)):
                return json.dumps(raw, default=str)[:500]
            return str(raw)[:500]
        except Exception:
            return None


_trace_store = SupportTracingProcessor()
try:
    add_trace_processor(_trace_store)
except Exception:
    pass

llm_provider = os.getenv("LLM_PROVIDER", "gemini")

_client = None
_model = None

try:
    if llm_provider == "ollama":
        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        ollama_model_name = os.getenv("OLLAMA_MODEL", "llama3.2")
        _client = openai.AsyncOpenAI(api_key="ollama", base_url=ollama_base)
        _model = OpenAIChatCompletionsModel(model=ollama_model_name, openai_client=_client)
    else:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            base_url = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
            model_name = os.getenv("MODEL", "gemini-2.5-flash")
            _client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
            _model = OpenAIChatCompletionsModel(model=model_name, openai_client=_client)
except Exception:
    pass

RETURN_POLICIES = {
    "electronics": "30-day return window. Items must be unopened. Restocking fee of 15% applies.",
    "clothing": "60-day return window. Items must have tags attached. Free returns.",
    "furniture": "14-day return window. Pickup fee may apply. Must be in original packaging.",
}

_tool_calls_log = []
_hook_events: list[dict] = []

# ── Guardrail & Validation Models ──

class GuardrailOutput(BaseModel):
    is_support_query: bool = True
    reasoning: str = ""

GUARDRAIL_INSTRUCTIONS = (
    "Determine if the user's message is a customer support query "
    "related to orders, tickets, return policies, account issues, "
    "product problems, shipping, billing, or escalation requests. "
    "If the user is asking about anything unrelated to support "
    "(e.g., general knowledge, creative writing, math, coding), "
    "set is_support_query to false."
)

guardrail_agent = Agent(
    name="Guardrail",
    instructions=GUARDRAIL_INSTRUCTIONS,
    model=_model,
    output_type=GuardrailOutput,
) if _model else None


@input_guardrail
async def support_guardrail(
    ctx: RunContextWrapper[SupportContext],
    agent: Agent,
    input_data: Union[str, list],
) -> GuardrailFunctionOutput:
    if guardrail_agent is None:
        return GuardrailFunctionOutput(
            output_info=GuardrailOutput(),
            tripwire_triggered=False,
        )
    text = input_data if isinstance(input_data, str) else str(input_data)
    result = await Runner.run(guardrail_agent, text, context=ctx.context)
    output = result.final_output
    return GuardrailFunctionOutput(
        output_info=output,
        tripwire_triggered=not output.is_support_query,
    )


# ── Output Quality Guardrail (Agent-Based Validation) ──

class OutputValidation(BaseModel):
    is_appropriate: bool = True
    reasoning: str = ""

OUTPUT_VALIDATOR_INSTRUCTIONS = (
    "You are a quality assurance validator for a support desk agent. "
    "Review the agent's response and determine if it is appropriate, "
    "helpful, and directly addresses the customer's support question. "
    "Set is_appropriate to false ONLY if the response is harmful, "
    "completely off-topic, nonsensical, or refuses to help without reason."
)

output_validator_agent = Agent(
    name="Output Validator",
    instructions=OUTPUT_VALIDATOR_INSTRUCTIONS,
    model=_model,
    output_type=OutputValidation,
) if _model else None


@output_guardrail
async def response_quality_guardrail(
    ctx: RunContextWrapper[SupportContext],
    agent: Agent,
    output: str,
) -> GuardrailFunctionOutput:
    if output_validator_agent is None:
        return GuardrailFunctionOutput(
            output_info=OutputValidation(),
            tripwire_triggered=False,
        )
    text = str(output)
    result = await Runner.run(output_validator_agent, text, context=ctx.context)
    validation = result.final_output
    return GuardrailFunctionOutput(
        output_info=validation,
        tripwire_triggered=not validation.is_appropriate,
    )


# ── Agent-Based Tool Input Validation ──

class ToolInputValidation(BaseModel):
    is_valid: bool = True
    reasoning: str = ""

AGENT_TOOL_VALIDATOR_INSTRUCTIONS = (
    "You validate tool inputs for a customer support desk system. "
    "Review the tool name and its arguments. Check that the input is "
    "complete, coherent, and appropriate for a support context. "
    "Set is_valid to false if the input is malicious, contains "
    "sensitive data (like API keys, passwords, or personal identifiable "
    "information beyond a name), is nonsensical, or clearly insufficient."
)

tool_input_validator_agent = Agent(
    name="Tool Input Validator",
    instructions=AGENT_TOOL_VALIDATOR_INSTRUCTIONS,
    model=_model,
    output_type=ToolInputValidation,
) if _model else None


@tool_input_guardrail
async def validate_escalation(data) -> ToolGuardrailFunctionOutput:
    if tool_input_validator_agent is None:
        return ToolGuardrailFunctionOutput.allow()
    args = json.loads(data.context.tool_arguments or "{}")
    prompt = (
        f"Validate this escalate_to_human tool call.\n"
        f"Arguments: {json.dumps(args)}\n\n"
        f"Requirements:\n"
        f"- customer_name must be a real-sounding name (at least 2 characters)\n"
        f"- issue_description must be detailed (at least 10 characters) and describe a real support issue\n"
        f"- No sensitive data like passwords, API keys, or credit card numbers\n"
    )
    result = await Runner.run(tool_input_validator_agent, prompt)
    output = result.final_output
    if not output.is_valid:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Validation failed: {output.reasoning}"
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_input_guardrail
async def validate_ticket_input(data) -> ToolGuardrailFunctionOutput:
    if tool_input_validator_agent is None:
        return ToolGuardrailFunctionOutput.allow()
    args = json.loads(data.context.tool_arguments or "{}")
    prompt = (
        f"Validate this create_ticket tool call.\n"
        f"Arguments: {json.dumps(args)}\n\n"
        f"Requirements:\n"
        f"- subject must be descriptive (at least 3 characters)\n"
        f"- description must be detailed (at least 10 characters)\n"
        f"- priority must be one of: low, medium, high, urgent\n"
        f"- No sensitive data like passwords, API keys, or credit card numbers\n"
    )
    result = await Runner.run(tool_input_validator_agent, prompt)
    output = result.final_output
    if not output.is_valid:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Validation failed: {output.reasoning}"
        )
    return ToolGuardrailFunctionOutput.allow()


# ── PII / Sensitive Data Output Guardrail ──

class PiiValidation(BaseModel):
    contains_pii: bool = False
    reasoning: str = ""

PII_VALIDATOR_INSTRUCTIONS = (
    "Review the following text for personally identifiable information (PII) "
    "or sensitive data that should not be exposed in a support chat response. "
    "Check for: credit card numbers, social security numbers, API keys, "
    "passwords, full addresses (beyond city/state), and full phone numbers. "
    "Set contains_pii to true if any such data is present."
)

pii_validator_agent = Agent(
    name="PII Validator",
    instructions=PII_VALIDATOR_INSTRUCTIONS,
    model=_model,
    output_type=PiiValidation,
) if _model else None


@output_guardrail
async def pii_output_guardrail(
    ctx: RunContextWrapper[SupportContext],
    agent: Agent,
    output: str,
) -> GuardrailFunctionOutput:
    if pii_validator_agent is None:
        return GuardrailFunctionOutput(
            output_info=PiiValidation(),
            tripwire_triggered=False,
        )
    text = str(output)
    result = await Runner.run(pii_validator_agent, text, context=ctx.context)
    validation = result.final_output
    return GuardrailFunctionOutput(
        output_info=validation,
        tripwire_triggered=validation.contains_pii,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Support Desk Agent", lifespan=lifespan)


@function_tool
def lookup_order_status(
    ctx: RunContextWrapper[SupportContext],
    order_id: str,
) -> str:
    """
    Look up the current status and details of a customer order by its unique order ID.

    Args:
        order_id: The unique order identifier to look up (e.g. ORD-1001, ORD-1002).

    Returns:
        A human-readable summary of the order including status, customer name,
        items purchased, total amount, order date, and estimated delivery date,
        or a message indicating the order was not found.
    """
    ctx.context.orders_looked_up += 1
    ctx.context.tools_called += 1
    order = db.get_order(order_id)
    if not order:
        return f"Order {order_id} not found. Please verify the order ID."
    result = (
        f"Order {order_id}: {order['status']}. "
        f"Customer: {order['customer']}. Items: {order['items']}. "
        f"Total: ${order['total']:.2f}. "
        f"Placed on {order['date']}, ETA: {order['eta']}."
    )
    _tool_calls_log.append({"tool": "lookup_order_status", "args": {"order_id": order_id}, "result": result})
    return result


@function_tool
def check_return_policy(
    ctx: RunContextWrapper[SupportContext],
    item_category: str,
) -> str:
    """
    Retrieve the return policy for a given product category.

    Args:
        item_category: The product category to check (e.g. electronics, clothing, furniture).

    Returns:
        The full return policy details including return window, condition requirements,
        and any applicable fees, or a message listing available categories if not found.
    """
    ctx.context.policies_checked += 1
    ctx.context.tools_called += 1
    policy = RETURN_POLICIES.get(item_category.lower())
    if not policy:
        result = f"Sorry, no return policy found for '{item_category}'. Available categories: {', '.join(RETURN_POLICIES.keys())}."
    else:
        result = f"Return policy for {item_category}: {policy}"
    _tool_calls_log.append({"tool": "check_return_policy", "args": {"item_category": item_category}, "result": result})
    return result


@function_tool
def check_ticket_status(
    ctx: RunContextWrapper[SupportContext],
    ticket_id: str,
) -> str:
    """
    Check the current status and details of a support ticket by its ticket ID.

    Args:
        ticket_id: The unique support ticket identifier (e.g. TKT-5001, TKT-5002).

    Returns:
        A summary of the ticket including its current status, priority level,
        the issue description, and the customer name, or a not-found message.
    """
    ctx.context.tickets_checked += 1
    ctx.context.tools_called += 1
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return f"Ticket {ticket_id} not found. Please verify the ticket ID."
    result = (
        f"Ticket {ticket_id}: {ticket['status']} (priority: {ticket['priority']}). "
        f"Issue: {ticket['issue']}. Customer: {ticket['customer']}."
    )
    _tool_calls_log.append({"tool": "check_ticket_status", "args": {"ticket_id": ticket_id}, "result": result})
    return result


@function_tool(tool_input_guardrails=[validate_escalation])
def escalate_to_human(
    ctx: RunContextWrapper[SupportContext],
    customer_name: str,
    issue_description: str,
) -> str:
    """
    Escalate a customer issue that the AI agent cannot resolve to a human support agent.

    Creates a new escalation ticket in the system for manual review.

    Args:
        customer_name: The full name of the customer requesting escalation (required).
        issue_description: A detailed description of the issue to escalate,
            including any relevant context or steps already taken (required).

    Returns:
        A confirmation message with the new escalation ticket ID and a note
        that a human agent will follow up within 24 hours.
    """
    ctx.context.escalations_created += 1
    ctx.context.tools_called += 1
    ticket_id = db.get_next_escalation_id()
    db.create_escalation(ticket_id, customer_name, issue_description)
    result = (
        f"Your issue has been escalated. A human agent will follow up within 24 hours. "
        f"Your escalation ticket ID is {ticket_id}."
    )
    _tool_calls_log.append({"tool": "escalate_to_human", "args": {"customer_name": customer_name, "issue_description": issue_description}, "result": result})
    return result


@function_tool
def get_session_stats(ctx: RunContextWrapper[SupportContext]) -> str:
    """
    Get statistics for the current support session, including how many
    orders, tickets, policies, and escalations have been handled.

    Returns:
        A summary string of all actions taken in this session.
    """
    return (
        f"Session stats for {ctx.context.username or 'agent'}: "
        f"{ctx.context.orders_looked_up} orders looked up, "
        f"{ctx.context.tickets_checked} tickets checked, "
        f"{ctx.context.policies_checked} policies reviewed, "
        f"{ctx.context.escalations_created} escalations created. "
        f"Total tools called: {ctx.context.tools_called}."
    )


@function_tool
def add_task(
    ctx: RunContextWrapper[SupportContext],
    title: str,
    priority: int = 1,
) -> str:
    """
    Add a new task to the session task list.

    Args:
        title: The task description (required).
        priority: Priority level 1-5 where 5 is highest (optional, defaults to 1).

    Returns:
        Confirmation message with the new task ID.
    """
    task_id = f"task_{len(ctx.context.tasks) + 1:03d}"
    task = {
        "id": task_id,
        "title": title,
        "priority": priority,
        "status": "pending",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    ctx.context.tasks.append(task)
    return f"Created {task_id}: '{title}' (priority {priority})"


@function_tool
def list_tasks(ctx: RunContextWrapper[SupportContext]) -> str:
    """
    List all tasks in the current session.

    Returns:
        A formatted list of all tasks with their status, priority, and ID.
    """
    tasks = ctx.context.tasks
    if not tasks:
        return "No tasks in the current session."
    lines = ["Current tasks:"]
    for t in tasks:
        status = "[x]" if t["status"] == "complete" else "[ ]"
        lines.append(f"  {status} {t['id']}: {t['title']} (P{t['priority']})")
    lines.append(f"\n{sum(1 for t in tasks if t['status'] == 'complete')}/{len(tasks)} tasks completed.")
    return "\n".join(lines)


@function_tool
def complete_task(
    ctx: RunContextWrapper[SupportContext],
    task_id: str,
) -> str:
    """
    Mark an existing task as complete.

    Args:
        task_id: The ID of the task to mark complete (e.g. task_001).

    Returns:
        Confirmation message showing the completed task title.
    """
    for task in ctx.context.tasks:
        if task["id"] == task_id:
            task["status"] = "complete"
            return f"Completed task {task_id}: '{task['title']}'"
    return f"Task {task_id} not found."


@function_tool(tool_input_guardrails=[validate_ticket_input])
def create_ticket(
    ctx: RunContextWrapper[SupportContext],
    subject: str,
    description: str,
    priority: str = "medium",
) -> str:
    """
    Create a new support ticket in the system.

    Args:
        subject: A short summary of the issue (required).
        description: A detailed description of the problem (required).
        priority: Priority level — low, medium, high, or urgent (optional, defaults to medium).

    Returns:
        Confirmation message with the new ticket ID.
    """
    ticket = db.create_ticket(ctx.context.username or "Unknown", f"{subject}: {description}", priority)
    ticket_id = ticket["id"]
    ctx.context.tickets.append({"id": ticket_id, "subject": subject, "priority": priority, "status": "open"})
    return f"Created ticket {ticket_id}: '{subject}' ({priority} priority). A support agent will review it shortly."


@function_tool
def check_account_status(ctx: RunContextWrapper[SupportContext]) -> str:
    """
    Check the account details and tier for the current customer.

    Returns:
        A summary of the customer's account including name, email, account tier,
        and any open tickets in this session.
    """
    name = ctx.context.username or "Unknown"
    email = ctx.context.email or "Not provided"
    tier = ctx.context.account_tier
    ticket_count = len(ctx.context.tickets)
    return (
        f"Account summary for {name} ({email}):\n"
        f"  Account tier: {tier.upper()}\n"
        f"  Tickets created this session: {ticket_count}\n"
        f"{'  You have priority support as a premium member.' if tier == 'premium' else '  Standard support hours: Mon-Fri 9am-6pm.'}"
    )


# ── Lifecycle Hooks ──

class LoggingHooks(AgentHooks):
    """Captures detailed agent lifecycle events for observability."""

    async def on_start(self, context, agent):
        _hook_events.append({
            "type": "agent_start",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_end(self, context, agent, output):
        _hook_events.append({
            "type": "agent_end",
            "agent": agent.name,
            "output": str(output)[:300],
            "timestamp": time.time(),
        })

    async def on_handoff(self, context, agent, handoff):
        _hook_events.append({
            "type": "handoff",
            "from_agent": agent.name,
            "to_agent": handoff.agent_name if hasattr(handoff, "agent_name") else str(handoff),
            "timestamp": time.time(),
        })

    async def on_llm_start(self, context, agent):
        _hook_events.append({
            "type": "llm_start",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_llm_end(self, context, agent, response):
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
                "completion_tokens": getattr(response.usage, "completion_tokens", None),
            }
        _hook_events.append({
            "type": "llm_end",
            "agent": agent.name,
            "usage": usage,
            "timestamp": time.time(),
        })

    async def on_tool_start(self, context, agent, tool_call):
        if context and hasattr(context, "tools_called"):
            context.tools_called += 1
        _tool_calls_log.append({
            "tool": tool_call.name,
            "args": tool_call.arguments if hasattr(tool_call, "arguments") else {},
        })
        _hook_events.append({
            "type": "tool_start",
            "agent": agent.name,
            "tool": tool_call.name,
            "timestamp": time.time(),
        })

    async def on_tool_end(self, context, agent, tool_call, result):
        for entry in _tool_calls_log:
            if entry["tool"] == tool_call.name and "result" not in entry:
                entry["result"] = str(result)[:500]
        _hook_events.append({
            "type": "tool_end",
            "agent": agent.name,
            "tool": tool_call.name,
            "result": str(result)[:300],
            "timestamp": time.time(),
        })


class SupportRunHooks(RunHooks):
    """Captures run-level lifecycle events."""

    async def on_agent_start(self, context, agent):
        _hook_events.append({
            "type": "run_agent_start",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_agent_end(self, context, agent, output):
        _hook_events.append({
            "type": "run_agent_end",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_handoff(self, context, agent, handoff):
        _hook_events.append({
            "type": "run_handoff",
            "from_agent": agent.name,
            "to_agent": handoff.agent_name if hasattr(handoff, "agent_name") else str(handoff),
            "timestamp": time.time(),
        })

    async def on_llm_start(self, context, agent):
        _hook_events.append({
            "type": "run_llm_start",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_llm_end(self, context, agent, response):
        _hook_events.append({
            "type": "run_llm_end",
            "agent": agent.name,
            "timestamp": time.time(),
        })

    async def on_tool_start(self, context, agent, tool_call):
        _hook_events.append({
            "type": "run_tool_start",
            "agent": agent.name,
            "tool": tool_call.name,
            "timestamp": time.time(),
        })

    async def on_tool_end(self, context, agent, tool_call, result):
        _hook_events.append({
            "type": "run_tool_end",
            "agent": agent.name,
            "tool": tool_call.name,
            "timestamp": time.time(),
        })


# ── Error Handlers ──

def on_max_turns(data):
    return {"final_output": "I need more turns to complete this request. Please try asking in shorter steps.", "include_in_history": True}


def on_refusal(data):
    return {"final_output": "I'm unable to process that request. Please ask a support-related question.", "include_in_history": True}

# ── Sub-Agents (Agents as Tools) ──

ORDERS_INSTRUCTIONS = (
    "You are an orders specialist. Use lookup_order_status to check order "
    "details and check_account_status to review the customer's account. "
    "Report back the information clearly. If an order is not found, say so."
)

POLICIES_TICKETS_INSTRUCTIONS = (
    "You are a policies and tickets specialist. Use check_return_policy to "
    "look up return policies by category (electronics, clothing, furniture). "
    "Use check_ticket_status to check existing support tickets, and "
    "create_ticket to create new support tickets. Be thorough."
)

ESCALATIONS_INSTRUCTIONS = (
    "You are an escalation specialist. When a customer's issue cannot be "
    "resolved by the support agent, use escalate_to_human to create an "
    "escalation ticket. Collect the customer's full name and a detailed "
    "issue description before escalating."
)

SESSION_INSTRUCTIONS = (
    "You manage session-level tools. Use get_session_stats to report on "
    "actions taken this session. Use add_task, list_tasks, and complete_task "
    "to manage the session task list."
)

orders_agent = Agent[SupportContext](
    name="Orders Specialist",
    handoff_description="Specialist for order status lookups and account status checks.",
    instructions=ORDERS_INSTRUCTIONS,
    tools=[lookup_order_status, check_account_status],
    model=_model,
    hooks=LoggingHooks(),
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

policies_tickets_agent = Agent[SupportContext](
    name="Policies & Tickets Specialist",
    handoff_description="Specialist for return policies, support ticket status, and creating new tickets.",
    instructions=POLICIES_TICKETS_INSTRUCTIONS,
    tools=[check_return_policy, check_ticket_status, create_ticket],
    model=_model,
    hooks=LoggingHooks(),
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

escalations_agent = Agent[SupportContext](
    name="Escalations Specialist",
    handoff_description="Specialist for escalating unresolved issues to human support agents.",
    instructions=ESCALATIONS_INSTRUCTIONS,
    tools=[escalate_to_human],
    model=_model,
    hooks=LoggingHooks(),
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

session_agent = Agent[SupportContext](
    name="Session Manager",
    handoff_description="Specialist for session statistics, task management, and productivity tools.",
    instructions=SESSION_INSTRUCTIONS,
    tools=[get_session_stats, add_task, list_tasks, complete_task],
    model=_model,
    hooks=LoggingHooks(),
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

# ── Orchestrator Agent (Manager Pattern) ──

SYSTEM_PROMPT = (
    "You are the main support desk orchestrator. Your job is to assist "
    "customers by delegating to your specialist sub-agents. Do NOT call "
    "function tools directly — use the appropriate sub-agent tool:\n\n"
    "1. orders_expert — for order status lookups and account status checks\n"
    "2. policies_tickets_expert — for return policies, ticket status checks, "
    "and creating new tickets\n"
    "3. escalation_expert — for escalating unresolved issues to a human agent\n"
    "4. session_manager — for session stats and task management\n\n"
    "You have access to context about the current user. Address them by "
    "their name when known.\n"
    "Be friendly, professional, and concise. If the customer seems frustrated, "
    "apologise and offer to escalate."
)

orchestrator_agent = Agent[SupportContext](
    name="Support Orchestrator",
    handoff_description="Handles complex queries spanning multiple support domains by delegating to specialist tools.",
    instructions=SYSTEM_PROMPT,
    tools=[
        orders_agent.as_tool(
            tool_name="orders_expert",
            tool_description="Handles order status lookups and account status checks. Use this for any order-related or account-related questions.",
        ),
        policies_tickets_agent.as_tool(
            tool_name="policies_tickets_expert",
            tool_description="Handles return policy questions, ticket status checks, and creating new support tickets. Use this for policy or ticket inquiries.",
        ),
        escalations_agent.as_tool(
            tool_name="escalation_expert",
            tool_description="Handles escalating issues to human support agents. Use this when a customer needs a human agent or their issue cannot be resolved.",
        ),
        session_agent.as_tool(
            tool_name="session_manager",
            tool_description="Manages session tasks and provides session statistics. Use this for task management or getting session stats.",
        ),
    ],
    model=_model,
    hooks=LoggingHooks(),
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

# ── Triage Agent (Handoffs Pattern) ──

TRIAGE_INSTRUCTIONS = (
    f"{RECOMMENDED_PROMPT_PREFIX}\n"
    "You are the front-line triage agent for a support desk. Your job is to:\n"
    "1. Greet the customer and understand their issue.\n"
    "2. Route them to the right specialist using handoffs.\n\n"
    "Available specialists — hand off to the most relevant one:\n"
    "- orders_agent: For order status lookups and account status checks.\n"
    "- policies_tickets_agent: For return policies, ticket status, and creating tickets.\n"
    "- escalation_agent: For escalating unresolved issues to human support.\n"
    "- session_agent: For session statistics and task management.\n"
    "- support_orchestrator: For complex queries spanning multiple support domains.\n\n"
    "If the query fits a single category clearly, hand off to that specialist. "
    "If it's complex or crosses multiple domains, hand off to the support orchestrator. "
    "Be friendly and professional."
)

triage_agent = Agent[SupportContext](
    name="Triage Agent",
    instructions=TRIAGE_INSTRUCTIONS,
    handoffs=[
        handoff(orders_agent, input_filter=handoff_filters.remove_all_tools),
        handoff(policies_tickets_agent, input_filter=handoff_filters.remove_all_tools),
        handoff(escalations_agent, input_filter=handoff_filters.remove_all_tools),
        handoff(session_agent, input_filter=handoff_filters.remove_all_tools),
        handoff(orchestrator_agent, input_filter=handoff_filters.remove_all_tools),
    ],
    model=_model,
    hooks=LoggingHooks(),
    input_guardrails=[support_guardrail],
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

# ── Run Agent ──

async def run_agent(question: str, conversation_id: str = None, ctx: SupportContext = None) -> dict:
    if not _client:
        err = "Error: No LLM configured. Set GEMINI_API_KEY or LLM_PROVIDER=ollama."
        return {"reply": err, "conversation_id": conversation_id}

    conversation_id = conversation_id or str(uuid.uuid4())
    global _tool_calls_log
    _tool_calls_log = []

    if ctx is None:
        ctx = SupportContext(conversation_id=conversation_id)
    else:
        ctx.conversation_id = conversation_id

    saved_state = db.load_conversation_state(conversation_id)
    if saved_state:
        try:
            saved_data = json.loads(saved_state)
            for key, value in saved_data.items():
                if hasattr(ctx, key) and key not in ("user_id", "username", "email", "account_tier", "conversation_id"):
                    setattr(ctx, key, value)
        except (json.JSONDecodeError, TypeError):
            pass

    session = SQLiteSession(
        session_id=conversation_id,
        db_path=db.DB_PATH,
        sessions_table="agent_sessions",
        messages_table="agent_messages",
    )

    run_config = RunConfig(
        workflow_name="Support Desk",
        tracing_disabled=False,
        hooks=[SupportRunHooks()],
    )

    try:
        async with trace(
            "Support Desk Run",
            group_id=conversation_id,
            metadata={
                "user_id": ctx.user_id,
                "username": ctx.username,
                "conversation_id": conversation_id,
            },
        ):
            result = await Runner.run(
                triage_agent,
                question,
                context=ctx,
                run_config=run_config,
                max_turns=8,
                error_handlers={
                    "max_turns": on_max_turns,
                    "model_refusal": on_refusal,
                },
                session=session,
            )
        reply = result.final_output

        db.save_conversation_state(conversation_id, json.dumps({
            "tools_called": ctx.tools_called,
            "orders_looked_up": ctx.orders_looked_up,
            "tickets_checked": ctx.tickets_checked,
            "policies_checked": ctx.policies_checked,
            "escalations_created": ctx.escalations_created,
            "tasks": ctx.tasks,
            "tickets": ctx.tickets,
        }))

        db.add_chat_message(conversation_id, "user", question)
        db.add_chat_message(conversation_id, "assistant", reply)

        return {"reply": reply, "conversation_id": conversation_id, "tool_calls": _tool_calls_log}
    except InputGuardrailTripwireTriggered:
        return {
            "reply": "I'm here to help with support-related questions — orders, tickets, return policies, and account issues. Could you ask something related to support?",
            "conversation_id": conversation_id,
            "tool_calls": _tool_calls_log,
        }
    except Exception as e:
        return {"reply": f"Sorry, I encountered an error: {str(e)}", "conversation_id": conversation_id, "tool_calls": _tool_calls_log}


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class OrderCreate(BaseModel):
    customer: str
    items: str
    total: float


class TicketCreate(BaseModel):
    customer: str
    issue: str
    priority: str = "medium"


class TicketUpdate(BaseModel):
    status: str
    assigned_to: Optional[str] = None


# ── Auth Endpoints ──


@app.post("/api/auth/register")
def register(req: RegisterRequest):
    existing = db.get_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = db.create_user(req.email, req.username, req.password)
    if not user:
        raise HTTPException(status_code=400, detail="Registration failed")
    token = create_access_token({"sub": user["id"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer", "user": UserOut(**user).model_dump()}


@app.post("/api/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = db.get_user_by_email(form_data.username)
    if not user or not db.verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token({"sub": user["id"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer", "user": UserOut(**user).model_dump()}


@app.get("/api/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return UserOut(**current_user).model_dump()


# ── API Routes (protected) ──


@app.get("/api/stats")
def get_stats(current_user: dict = Depends(get_current_user)):
    return db.get_stats()


@app.get("/api/orders")
def get_orders(current_user: dict = Depends(get_current_user)):
    return db.get_all_orders()


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, current_user: dict = Depends(get_current_user)):
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/api/orders")
def create_order(order: OrderCreate, current_user: dict = Depends(get_current_user)):
    return db.create_order(order.customer, order.items, order.total)


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: str, current_user: dict = Depends(get_current_user)):
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    db.delete_order(order_id)
    return {"message": f"Order {order_id} deleted"}


@app.get("/api/tickets")
def get_tickets(current_user: dict = Depends(get_current_user)):
    return db.get_all_tickets()


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str, current_user: dict = Depends(get_current_user)):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.post("/api/tickets")
def create_ticket(ticket: TicketCreate, current_user: dict = Depends(get_current_user)):
    return db.create_ticket(ticket.customer, ticket.issue, ticket.priority)


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(ticket_id: str, update: TicketUpdate, current_user: dict = Depends(get_current_user)):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return db.update_ticket(ticket_id, update.status, update.assigned_to)


@app.get("/api/escalations")
def get_escalations(current_user: dict = Depends(get_current_user)):
    return db.get_all_escalations()


@app.post("/api/escalations/{ticket_id}/resolve")
def resolve_escalation(ticket_id: str, current_user: dict = Depends(get_current_user)):
    db.resolve_escalation(ticket_id)
    return {"message": f"Escalation {ticket_id} resolved"}


@app.get("/api/customers")
def get_customers(current_user: dict = Depends(get_current_user)):
    return db.get_all_customers()


@app.get("/api/return-policies")
def get_return_policies(current_user: dict = Depends(get_current_user)):
    return RETURN_POLICIES


@app.post("/api/chat")
async def chat(req: ChatRequest, current_user: dict = Depends(get_current_user)):
    ctx = SupportContext(
        user_id=current_user["id"],
        username=current_user["username"],
        email=current_user["email"],
        account_tier=current_user.get("role", "standard"),
        conversation_id=req.conversation_id,
    )
    return await run_agent(req.message, req.conversation_id, ctx=ctx)


@app.get("/api/chat-history")
def get_chat_history(current_user: dict = Depends(get_current_user)):
    return db.get_recent_chat_history(50)


# ── Observability Endpoints ──


@app.get("/api/observability/traces")
def get_traces(
    current_user: dict = Depends(get_current_user),
    limit: int = Query(10, ge=1, le=100),
):
    return {
        "traces": list(reversed(_trace_store.traces))[:limit],
        "spans": list(reversed(_trace_store.spans))[:limit * 5],
        "total_traces": len(_trace_store.traces),
        "total_spans": len(_trace_store.spans),
    }


@app.get("/api/observability/events")
def get_hook_events(
    current_user: dict = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=500),
):
    return {
        "events": list(reversed(_hook_events))[:limit],
        "total_events": len(_hook_events),
    }


@app.get("/api/observability/stats")
def get_observability_stats(current_user: dict = Depends(get_current_user)):
    agent_starts = sum(1 for e in _hook_events if e["type"] == "agent_start")
    tool_calls = sum(1 for e in _hook_events if e["type"] == "tool_start")
    handoffs = sum(1 for e in _hook_events if e["type"] == "handoff")
    llm_calls = sum(1 for e in _hook_events if e["type"] == "llm_start")
    errors = sum(1 for s in _trace_store.spans if s.get("error"))
    return {
        "agent_starts": agent_starts,
        "tool_calls": tool_calls,
        "handoffs": handoffs,
        "llm_calls": llm_calls,
        "traces": len(_trace_store.traces),
        "spans": len(_trace_store.spans),
        "span_errors": errors,
        "hook_events": len(_hook_events),
    }


@app.post("/api/observability/reset")
def reset_observability(current_user: dict = Depends(get_current_user)):
    _trace_store.traces.clear()
    _trace_store.spans.clear()
    _trace_store._trace_map.clear()
    _hook_events.clear()
    return {"message": "Observability data cleared"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
