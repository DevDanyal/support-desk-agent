import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

ORDERS_DB = {
    "ORD-1001": {"status": "shipped", "date": "2026-06-01", "eta": "2026-06-10"},
    "ORD-1002": {"status": "processing", "date": "2026-06-05", "eta": "2026-06-15"},
    "ORD-1003": {"status": "delivered", "date": "2026-05-20", "eta": "2026-05-28"},
}

TICKETS_DB = {
    "TKT-5001": {"status": "open", "priority": "high", "issue": "Wrong item received"},
    "TKT-5002": {"status": "in_progress", "priority": "medium", "issue": "Refund not processed"},
    "TKT-5003": {"status": "resolved", "priority": "low", "issue": "Shipping address change"},
}

ESCALATION_QUEUE = []


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
        f"Issue: {ticket['issue']}."
    )


def escalate_to_human(customer_name: str, issue_description: str) -> str:
    ticket_id = f"TKT-{len(ESCALATION_QUEUE) + 6000}"
    ESCALATION_QUEUE.append({
        "ticket_id": ticket_id,
        "customer": customer_name,
        "issue": issue_description,
    })
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


def main():
    print("Support Desk Agent")
    print("=" * 50)

    api_key = os.getenv("GEMINI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    model = os.getenv("MODEL", "gemini-2.0-flash")

    if not api_key:
        print("Error: GEMINI_API_KEY not set in .env file.")
        return

    client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(question: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        for _ in range(5):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )

            choice = response.choices[0]
            if choice.finish_reason == "stop":
                return choice.message.content

            if choice.finish_reason == "tool_calls":
                messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn = tool_call.function
                    handler = TOOL_MAP.get(fn.name)
                    if handler:
                        args = json.loads(fn.arguments)
                        result = handler(**args)
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

        return "Sorry, I couldn't process your request."

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"\nYou: {question}")
        answer = chat(question)
        print(f"\nAgent: {answer}")
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
                answer = chat(user_input)
                print(f"\nAgent: {answer}\n")
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break


if __name__ == "__main__":
    main()
