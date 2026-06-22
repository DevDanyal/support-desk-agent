import asyncio
import json
import os
import sys
import openai
from datetime import datetime, timezone
from typing import Optional, Union
from dotenv import load_dotenv
from pydantic import BaseModel
from agents import (
    Agent, Runner, function_tool, set_tracing_disabled,
    GuardrailFunctionOutput, InputGuardrailTripwireTriggered,
    RunContextWrapper, AgentHooks, RunConfig, input_guardrail,
    output_guardrail, tool_input_guardrail,
    ToolGuardrailFunctionOutput, handoff, SQLiteSession,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.extensions import handoff_filters
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
import database as db

load_dotenv(override=True)

set_tracing_disabled(True)

llm_provider = os.getenv("LLM_PROVIDER", "gemini")

if llm_provider == "ollama":
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    ollama_model_name = os.getenv("OLLAMA_MODEL", "llama3.2")
    _client = openai.AsyncOpenAI(api_key="ollama", base_url=ollama_base)
    _model = OpenAIChatCompletionsModel(model=ollama_model_name, openai_client=_client)
else:
    api_key = os.getenv("GEMINI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    model_name = os.getenv("MODEL", "gemini-2.5-flash")
    _client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url) if api_key else None
    _model = OpenAIChatCompletionsModel(model=model_name, openai_client=_client) if _client else None


class SupportContext(BaseModel):
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


RETURN_POLICIES = {
    "electronics": "30-day return window. Items must be unopened. Restocking fee of 15% applies.",
    "clothing": "60-day return window. Items must have tags attached. Free returns.",
    "furniture": "14-day return window. Pickup fee may apply. Must be in original packaging.",
}

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
    return (
        f"Order {order_id}: {order['status']}. "
        f"Customer: {order['customer']}. Items: {order['items']}. "
        f"Total: ${order['total']:.2f}. "
        f"Placed on {order['date']}, ETA: {order['eta']}."
    )


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
        return f"Sorry, no return policy found for '{item_category}'. Available categories: {', '.join(RETURN_POLICIES.keys())}."
    return f"Return policy for {item_category}: {policy}"


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
    return (
        f"Ticket {ticket_id}: {ticket['status']} (priority: {ticket['priority']}). "
        f"Issue: {ticket['issue']}. Customer: {ticket['customer']}."
    )


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
    return (
        f"Your issue has been escalated. A human agent will follow up within 24 hours. "
        f"Your escalation ticket ID is {ticket_id}."
    )


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
    async def on_agent_start(self, context, agent):
        pass
    async def on_llm_end(self, context, agent, response):
        pass
    async def on_tool_start(self, context, agent, tool_call):
        if context and hasattr(context, "tools_called"):
            context.tools_called += 1
    async def on_tool_end(self, context, agent, tool_call, result):
        pass


# ── Error Handlers ──

def on_max_turns(data):
    return {"final_output": "I need more turns to complete this request. Please try asking in shorter steps.", "include_in_history": True}

def on_refusal(data):
    return {"final_output": "I'm unable to process that request. Please ask a support-related question.", "include_in_history": True}


# ── Sub-Agents (Specialists) ──

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
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

policies_tickets_agent = Agent[SupportContext](
    name="Policies & Tickets Specialist",
    handoff_description="Specialist for return policies, support ticket status, and creating new tickets.",
    instructions=POLICIES_TICKETS_INSTRUCTIONS,
    tools=[check_return_policy, check_ticket_status, create_ticket],
    model=_model,
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

escalations_agent = Agent[SupportContext](
    name="Escalations Specialist",
    handoff_description="Specialist for escalating unresolved issues to human support agents.",
    instructions=ESCALATIONS_INSTRUCTIONS,
    tools=[escalate_to_human],
    model=_model,
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)

session_agent = Agent[SupportContext](
    name="Session Manager",
    handoff_description="Specialist for session statistics, task management, and productivity tools.",
    instructions=SESSION_INSTRUCTIONS,
    tools=[get_session_stats, add_task, list_tasks, complete_task],
    model=_model,
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)


# ── Orchestrator Agent (Manager Pattern — Agents as Tools) ──

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


# ── Triage Agent (Handoffs + Message Filtering) ──

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
    input_guardrails=[support_guardrail],
    output_guardrails=[response_quality_guardrail, pii_output_guardrail],
)


# ── Main ──

async def main():
    print("Support Desk Agent")
    print("=" * 50)

    if not _client:
        print("Error: No LLM configured. Set GEMINI_API_KEY or LLM_PROVIDER=ollama.")
        return

    db.init_db()

    run_config = RunConfig(
        workflow_name="Support Desk",
        tracing_disabled=True,
    )

    conversation_id = "cli_" + str(int(datetime.now().timestamp()))
    ctx = SupportContext(conversation_id=conversation_id)

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

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"\nYou: {question}")
        try:
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
            db.save_conversation_state(conversation_id, json.dumps({
                "tools_called": ctx.tools_called,
                "orders_looked_up": ctx.orders_looked_up,
                "tickets_checked": ctx.tickets_checked,
                "policies_checked": ctx.policies_checked,
                "escalations_created": ctx.escalations_created,
                "tasks": ctx.tasks,
                "tickets": ctx.tickets,
            }))
            print(f"\nAgent: {result.final_output}\n")
        except InputGuardrailTripwireTriggered:
            print("\nAgent: I'm here to help with support-related questions — orders, tickets, return policies, and account issues. Could you ask something related to support?\n")
        except Exception as e:
            print(f"\nError: {e}\n")
    else:
        print("Type 'exit' or 'quit' to end the conversation.\n")
        while True:
            try:
                user_input = input("You: ").strip()
                if user_input.lower() in ("exit", "quit"):
                    print("Goodbye!")
                    break
                if not user_input:
                    continue
                result = await Runner.run(
                    triage_agent,
                    user_input,
                    context=ctx,
                    run_config=run_config,
                    max_turns=8,
                    error_handlers={
                        "max_turns": on_max_turns,
                        "model_refusal": on_refusal,
                    },
                    session=session,
                )
                db.save_conversation_state(conversation_id, json.dumps({
                    "tools_called": ctx.tools_called,
                    "orders_looked_up": ctx.orders_looked_up,
                    "tickets_checked": ctx.tickets_checked,
                    "policies_checked": ctx.policies_checked,
                    "escalations_created": ctx.escalations_created,
                    "tasks": ctx.tasks,
                    "tickets": ctx.tickets,
                }))
                print(f"\nAgent: {result.final_output}\n")
            except InputGuardrailTripwireTriggered:
                print("\nAgent: I'm here to help with support-related questions — orders, tickets, return policies, and account issues. Could you ask something related to support?\n")
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            except Exception as e:
                print(f"\nError: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
