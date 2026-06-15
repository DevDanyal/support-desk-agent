import os
import uuid
import uvicorn
import openai
from contextlib import asynccontextmanager
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from agents import (
    Agent, Runner, function_tool, set_tracing_disabled,
    GuardrailFunctionOutput, InputGuardrailTripwireTriggered,
    RunContextWrapper, AgentHooks, RunConfig, input_guardrail,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
import database as db

load_dotenv(override=True)
try:
    set_tracing_disabled(True)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Support Desk Agent", lifespan=lifespan)


@function_tool
def lookup_order_status(order_id: str) -> str:
    """Look up the status of a customer order by order ID.

    Args:
        order_id: The order ID to look up (e.g. ORD-1001).
    """
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
def check_return_policy(item_category: str) -> str:
    """Check the return policy for a given item category.

    Args:
        item_category: The category of the item (e.g. electronics, clothing, furniture).
    """
    policy = RETURN_POLICIES.get(item_category.lower())
    if not policy:
        result = f"Sorry, no return policy found for '{item_category}'. Available categories: {', '.join(RETURN_POLICIES.keys())}."
    else:
        result = f"Return policy for {item_category}: {policy}"
    _tool_calls_log.append({"tool": "check_return_policy", "args": {"item_category": item_category}, "result": result})
    return result


@function_tool
def check_ticket_status(ticket_id: str) -> str:
    """Check the status of a support ticket by ticket ID.

    Args:
        ticket_id: The ticket ID to look up (e.g. TKT-5001).
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return f"Ticket {ticket_id} not found. Please verify the ticket ID."
    result = (
        f"Ticket {ticket_id}: {ticket['status']} (priority: {ticket['priority']}). "
        f"Issue: {ticket['issue']}. Customer: {ticket['customer']}."
    )
    _tool_calls_log.append({"tool": "check_ticket_status", "args": {"ticket_id": ticket_id}, "result": result})
    return result


@function_tool
def escalate_to_human(customer_name: str, issue_description: str) -> str:
    """Escalate a customer issue to a human support agent.

    Args:
        customer_name: The name of the customer.
        issue_description: A description of the issue to escalate.
    """
    ticket_id = db.get_next_escalation_id()
    db.create_escalation(ticket_id, customer_name, issue_description)
    result = (
        f"Your issue has been escalated. A human agent will follow up within 24 hours. "
        f"Your escalation ticket ID is {ticket_id}."
    )
    _tool_calls_log.append({"tool": "escalate_to_human", "args": {"customer_name": customer_name, "issue_description": issue_description}, "result": result})
    return result


# ── Guardrail Agent ──

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
    ctx: RunContextWrapper,
    agent: Agent,
    input_data: str | list,
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

# ── Lifecycle Hooks ──

class LoggingHooks(AgentHooks):
    async def on_agent_start(self, context, agent):
        pass

    async def on_llm_end(self, context, agent, response):
        pass

    async def on_tool_start(self, context, agent, tool_call):
        _tool_calls_log.append({
            "tool": tool_call.name,
            "args": tool_call.arguments if hasattr(tool_call, "arguments") else {},
        })

    async def on_tool_end(self, context, agent, tool_call, result):
        for entry in _tool_calls_log:
            if entry["tool"] == tool_call.name and "result" not in entry:
                entry["result"] = str(result)[:500]

# ── Error Handlers ──

def on_max_turns(data):
    return {"final_output": "I need more turns to complete this request. Please try asking in shorter steps.", "include_in_history": True}


def on_refusal(data):
    return {"final_output": "I'm unable to process that request. Please ask a support-related question.", "include_in_history": True}

# ── Main Agent ──

SYSTEM_PROMPT = (
    "You are a helpful support desk agent. Your job is to assist customers with:\n"
    "1. Looking up order statuses (use lookup_order_status)\n"
    "2. Checking return policies (use check_return_policy)\n"
    "3. Checking support ticket statuses (use check_ticket_status)\n"
    "4. Escalating unresolved issues to a human agent (use escalate_to_human)\n\n"
    "Be friendly, professional, and concise. If the customer seems frustrated, "
    "apologise and offer to escalate."
)

agent = Agent(
    name="Support Agent",
    instructions=SYSTEM_PROMPT,
    tools=[lookup_order_status, check_return_policy, check_ticket_status, escalate_to_human],
    model=_model,
    input_guardrails=[support_guardrail],
    hooks=LoggingHooks(),
)

# ── Run Agent ──

async def run_agent(question: str, conversation_id: str = None) -> dict:
    if not _client:
        err = "Error: No LLM configured. Set GEMINI_API_KEY or LLM_PROVIDER=ollama."
        return {"reply": err, "conversation_id": conversation_id}

    conversation_id = conversation_id or str(uuid.uuid4())
    global _tool_calls_log
    _tool_calls_log = []

    run_config = RunConfig(
        workflow_name="Support Desk",
        tracing_disabled=True,
    )

    try:
        history = db.get_conversation_history(conversation_id)

        if history:
            messages = []
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": question})
            agent_input = messages
        else:
            agent_input = question

        result = await Runner.run(
            agent,
            agent_input,
            run_config=run_config,
            max_turns=8,
            error_handlers={
                "max_turns": on_max_turns,
                "model_refusal": on_refusal,
            },
        )
        reply = result.final_output

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


@app.get("/api/stats")
def get_stats():
    return db.get_stats()


@app.get("/api/orders")
def get_orders():
    return db.get_all_orders()


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post("/api/orders")
def create_order(order: OrderCreate):
    return db.create_order(order.customer, order.items, order.total)


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: str):
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    db.delete_order(order_id)
    return {"message": f"Order {order_id} deleted"}


@app.get("/api/tickets")
def get_tickets():
    return db.get_all_tickets()


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.post("/api/tickets")
def create_ticket(ticket: TicketCreate):
    return db.create_ticket(ticket.customer, ticket.issue, ticket.priority)


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(ticket_id: str, update: TicketUpdate):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return db.update_ticket(ticket_id, update.status, update.assigned_to)


@app.get("/api/escalations")
def get_escalations():
    return db.get_all_escalations()


@app.post("/api/escalations/{ticket_id}/resolve")
def resolve_escalation(ticket_id: str):
    db.resolve_escalation(ticket_id)
    return {"message": f"Escalation {ticket_id} resolved"}


@app.get("/api/customers")
def get_customers():
    return db.get_all_customers()


@app.get("/api/return-policies")
def get_return_policies():
    return RETURN_POLICIES


@app.post("/api/chat")
async def chat(req: ChatRequest):
    return await run_agent(req.message, req.conversation_id)


@app.get("/api/chat-history")
def get_chat_history():
    return db.get_recent_chat_history(50)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
