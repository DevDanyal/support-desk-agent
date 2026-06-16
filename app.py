import os
import uuid
import uvicorn
import openai
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel
from agents import (
    Agent, Runner, function_tool, set_tracing_disabled,
    GuardrailFunctionOutput, InputGuardrailTripwireTriggered,
    RunContextWrapper, AgentHooks, RunConfig, input_guardrail,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
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


@function_tool
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


@function_tool
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

# ── Lifecycle Hooks ──

class LoggingHooks(AgentHooks):
    async def on_agent_start(self, context, agent):
        pass

    async def on_llm_end(self, context, agent, response):
        pass

    async def on_tool_start(self, context, agent, tool_call):
        if context and hasattr(context, "tools_called"):
            context.tools_called += 1
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
    "4. Escalating unresolved issues to a human agent (use escalate_to_human)\n"
    "5. Getting session statistics and action history (use get_session_stats)\n"
    "6. Managing session tasks — add tasks (use add_task), list them (use list_tasks), mark complete (use complete_task)\n"
    "7. Creating support tickets in the system (use create_ticket)\n"
    "8. Checking the current customer's account status and tier (use check_account_status)\n\n"
    "You have access to context about the current user. Address them by their name when known.\n"
    "Be friendly, professional, and concise. If the customer seems frustrated, "
    "apologise and offer to escalate."
)

agent = Agent[SupportContext](
    name="Support Agent",
    instructions=SYSTEM_PROMPT,
    tools=[lookup_order_status, check_return_policy, check_ticket_status, escalate_to_human, get_session_stats, add_task, list_tasks, complete_task, create_ticket, check_account_status],
    model=_model,
    input_guardrails=[support_guardrail],
    hooks=LoggingHooks(),
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
        ctx.tools_called = 0

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
            context=ctx,
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
