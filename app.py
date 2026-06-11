import os
import sys
import json
import uuid
import uvicorn
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv(override=True)

ORDERS_DB = {
    "ORD-1001": {"status": "shipped", "date": "2026-06-01", "eta": "2026-06-10", "customer": "Alice Johnson", "items": "Wireless Headphones", "total": 89.99},
    "ORD-1002": {"status": "processing", "date": "2026-06-05", "eta": "2026-06-15", "customer": "Bob Smith", "items": "USB-C Hub, Mouse Pad", "total": 34.50},
    "ORD-1003": {"status": "delivered", "date": "2026-05-20", "eta": "2026-05-28", "customer": "Carol Davis", "items": "Mechanical Keyboard", "total": 149.99},
    "ORD-1004": {"status": "cancelled", "date": "2026-05-25", "eta": "N/A", "customer": "David Wilson", "items": "Monitor Stand", "total": 45.00},
    "ORD-1005": {"status": "processing", "date": "2026-06-07", "eta": "2026-06-17", "customer": "Eve Martinez", "items": "Webcam, Microphone", "total": 129.99},
}

TICKETS_DB = {
    "TKT-5001": {"status": "open", "priority": "high", "issue": "Wrong item received", "customer": "Alice Johnson", "date": "2026-06-02", "assigned_to": "Unassigned"},
    "TKT-5002": {"status": "in_progress", "priority": "medium", "issue": "Refund not processed", "customer": "Bob Smith", "date": "2026-06-06", "assigned_to": "Sarah Chen"},
    "TKT-5003": {"status": "resolved", "priority": "low", "issue": "Shipping address change", "customer": "Carol Davis", "date": "2026-05-22", "assigned_to": "Mike Ross"},
    "TKT-5004": {"status": "open", "priority": "urgent", "issue": "Account hacked - unauthorized purchases", "customer": "Frank Lee", "date": "2026-06-08", "assigned_to": "Unassigned"},
    "TKT-5005": {"status": "open", "priority": "medium", "issue": "Damaged product on delivery", "customer": "Grace Kim", "date": "2026-06-09", "assigned_to": "Unassigned"},
}

ESCALATION_QUEUE = [
    {"ticket_id": "TKT-5004", "customer": "Frank Lee", "issue": "Account hacked - unauthorized purchases", "escalated_at": "2026-06-08 14:30", "status": "pending"},
]

CUSTOMERS_DB = {
    "CST-001": {"name": "Alice Johnson", "email": "alice@email.com", "orders": 3, "tickets": 1, "member_since": "2025-03-15"},
    "CST-002": {"name": "Bob Smith", "email": "bob@email.com", "orders": 1, "tickets": 1, "member_since": "2025-06-20"},
    "CST-003": {"name": "Carol Davis", "email": "carol@email.com", "orders": 5, "tickets": 1, "member_since": "2024-11-01"},
    "CST-004": {"name": "David Wilson", "email": "david@email.com", "orders": 2, "tickets": 0, "member_since": "2025-08-12"},
    "CST-005": {"name": "Eve Martinez", "email": "eve@email.com", "orders": 4, "tickets": 0, "member_since": "2025-01-05"},
}

ACTIVITY_LOG = []

CHAT_HISTORY = []

app = FastAPI(title="Support Desk Agent")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order_status",
            "description": "Look up the status of a customer order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID to look up (e.g. ORD-1001).",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_return_policy",
            "description": "Check the return policy for a given item category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_category": {
                        "type": "string",
                        "description": "The category of the item (e.g. electronics, clothing, furniture).",
                    }
                },
                "required": ["item_category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_ticket_status",
            "description": "Check the status of a support ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The ticket ID to look up (e.g. TKT-5001).",
                    }
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Escalate a customer issue to a human support agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "The name of the customer.",
                    },
                    "issue_description": {
                        "type": "string",
                        "description": "A description of the issue to escalate.",
                    },
                },
                "required": ["customer_name", "issue_description"],
            },
        },
    },
]


def lookup_order_status(order_id: str) -> str:
    order = ORDERS_DB.get(order_id)
    if not order:
        return f"Order {order_id} not found. Please verify the order ID."
    return (
        f"Order {order_id}: {order['status']}. "
        f"Customer: {order['customer']}. Items: {order['items']}. "
        f"Total: ${order['total']:.2f}. "
        f"Placed on {order['date']}, ETA: {order['eta']}."
    )


def check_return_policy(item_category: str) -> str:
    policies = {
        "electronics": "30-day return window. Items must be unopened. Restocking fee of 15% applies.",
        "clothing": "60-day return window. Items must have tags attached. Free returns.",
        "furniture": "14-day return window. Pickup fee may apply. Must be in original packaging.",
    }
    policy = policies.get(item_category.lower())
    if not policy:
        return f"Sorry, no return policy found for '{item_category}'. Available categories: {', '.join(policies.keys())}."
    return f"Return policy for {item_category}: {policy}"


def check_ticket_status(ticket_id: str) -> str:
    ticket = TICKETS_DB.get(ticket_id)
    if not ticket:
        return f"Ticket {ticket_id} not found. Please verify the ticket ID."
    return (
        f"Ticket {ticket_id}: {ticket['status']} (priority: {ticket['priority']}). "
        f"Issue: {ticket['issue']}. Customer: {ticket['customer']}."
    )


def escalate_to_human(customer_name: str, issue_description: str) -> str:
    ticket_id = f"TKT-{len(ESCALATION_QUEUE) + 6000}"
    esc_entry = {
        "ticket_id": ticket_id,
        "customer": customer_name,
        "issue": issue_description,
        "escalated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }
    ESCALATION_QUEUE.append(esc_entry)
    return (
        f"Your issue has been escalated. A human agent will follow up within 24 hours. "
        f"Your escalation ticket ID is {ticket_id}."
    )


TOOL_MAP = {
    "lookup_order_status": lookup_order_status,
    "check_return_policy": check_return_policy,
    "check_ticket_status": check_ticket_status,
    "escalate_to_human": escalate_to_human,
}

SYSTEM_PROMPT = (
    "You are a helpful support desk agent. Your job is to assist customers with:\n"
    "1. Looking up order statuses (use lookup_order_status)\n"
    "2. Checking return policies (use check_return_policy)\n"
    "3. Checking support ticket statuses (use check_ticket_status)\n"
    "4. Escalating unresolved issues to a human agent (use escalate_to_human)\n\n"
    "Be friendly, professional, and concise. If the customer seems frustrated, "
    "apologise and offer to escalate."
)

api_key = os.getenv("GEMINI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
model = os.getenv("MODEL", "gemini-2.0-flash")

client = OpenAI(api_key=api_key, base_url=base_url) if api_key else None


def run_agent(question: str, conversation_id: str = None) -> dict:
    if not client:
        return {"reply": "Error: GEMINI_API_KEY not configured. Please set it in the .env file.", "conversation_id": conversation_id}

    conversation_id = conversation_id or str(uuid.uuid4())

    history = [m for m in CHAT_HISTORY if m["conversation_id"] == conversation_id][-10:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": question})

    tool_calls_made = []

    for _ in range(5):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        choice = response.choices[0]
        if choice.finish_reason == "stop":
            reply = choice.message.content
            CHAT_HISTORY.append({"conversation_id": conversation_id, "role": "user", "content": question})
            CHAT_HISTORY.append({"conversation_id": conversation_id, "role": "assistant", "content": reply})
            return {"reply": reply, "conversation_id": conversation_id, "tool_calls": tool_calls_made}

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)
            for tool_call in choice.message.tool_calls:
                fn = tool_call.function
                handler = TOOL_MAP.get(fn.name)
                if handler:
                    args = json.loads(fn.arguments)
                    result = handler(**args)
                    tool_calls_made.append({"tool": fn.name, "args": args, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"Unknown tool: {fn.name}",
                    })

    return {"reply": "Sorry, I couldn't process your request.", "conversation_id": conversation_id, "tool_calls": tool_calls_made}


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
    order_statuses = {}
    for o in ORDERS_DB.values():
        order_statuses[o["status"]] = order_statuses.get(o["status"], 0) + 1

    ticket_statuses = {}
    for t in TICKETS_DB.values():
        ticket_statuses[t["status"]] = ticket_statuses.get(t["status"], 0) + 1

    priority_counts = {}
    for t in TICKETS_DB.values():
        priority_counts[t["priority"]] = priority_counts.get(t["priority"], 0) + 1

    return {
        "total_orders": len(ORDERS_DB),
        "total_tickets": len(TICKETS_DB),
        "total_customers": len(CUSTOMERS_DB),
        "pending_escalations": sum(1 for e in ESCALATION_QUEUE if e["status"] == "pending"),
        "order_statuses": order_statuses,
        "ticket_statuses": ticket_statuses,
        "priority_counts": priority_counts,
        "revenue": sum(o["total"] for o in ORDERS_DB.values()),
    }


@app.get("/api/orders")
def get_orders():
    return [{"id": k, **v} for k, v in ORDERS_DB.items()]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    order = ORDERS_DB.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"id": order_id, **order}


@app.post("/api/orders")
def create_order(order: OrderCreate):
    order_id = f"ORD-{len(ORDERS_DB) + 2000}"
    ORDERS_DB[order_id] = {
        "status": "processing",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "eta": (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d"),
        "customer": order.customer,
        "items": order.items,
        "total": order.total,
    }
    return {"id": order_id, **ORDERS_DB[order_id]}


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: str):
    if order_id not in ORDERS_DB:
        raise HTTPException(status_code=404, detail="Order not found")
    del ORDERS_DB[order_id]
    return {"message": f"Order {order_id} deleted"}


@app.get("/api/tickets")
def get_tickets():
    return [{"id": k, **v} for k, v in TICKETS_DB.items()]


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    ticket = TICKETS_DB.get(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"id": ticket_id, **ticket}


@app.post("/api/tickets")
def create_ticket(ticket: TicketCreate):
    ticket_id = f"TKT-{len(TICKETS_DB) + 6000}"
    TICKETS_DB[ticket_id] = {
        "status": "open",
        "priority": ticket.priority,
        "issue": ticket.issue,
        "customer": ticket.customer,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "assigned_to": "Unassigned",
    }
    return {"id": ticket_id, **TICKETS_DB[ticket_id]}


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(ticket_id: str, update: TicketUpdate):
    if ticket_id not in TICKETS_DB:
        raise HTTPException(status_code=404, detail="Ticket not found")
    TICKETS_DB[ticket_id]["status"] = update.status
    if update.assigned_to:
        TICKETS_DB[ticket_id]["assigned_to"] = update.assigned_to
    return {"id": ticket_id, **TICKETS_DB[ticket_id]}


@app.get("/api/escalations")
def get_escalations():
    return ESCALATION_QUEUE


@app.post("/api/escalations/{ticket_id}/resolve")
def resolve_escalation(ticket_id: str):
    for esc in ESCALATION_QUEUE:
        if esc["ticket_id"] == ticket_id:
            esc["status"] = "resolved"
            return {"message": f"Escalation {ticket_id} resolved"}
    raise HTTPException(status_code=404, detail="Escalation not found")


@app.get("/api/customers")
def get_customers():
    return [{"id": k, **v} for k, v in CUSTOMERS_DB.items()]


@app.get("/api/return-policies")
def get_return_policies():
    return {
        "electronics": "30-day return window. Items must be unopened. Restocking fee of 15% applies.",
        "clothing": "60-day return window. Items must have tags attached. Free returns.",
        "furniture": "14-day return window. Pickup fee may apply. Must be in original packaging.",
    }


@app.post("/api/chat")
def chat(req: ChatRequest):
    result = run_agent(req.message, req.conversation_id)
    return result


@app.get("/api/chat-history")
def get_chat_history():
    return CHAT_HISTORY[-50:]


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
