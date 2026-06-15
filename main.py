import os
import sys
import openai
from dotenv import load_dotenv
from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
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

RETURN_POLICIES = {
    "electronics": "30-day return window. Items must be unopened. Restocking fee of 15% applies.",
    "clothing": "60-day return window. Items must have tags attached. Free returns.",
    "furniture": "14-day return window. Pickup fee may apply. Must be in original packaging.",
}


@function_tool
def lookup_order_status(order_id: str) -> str:
    """Look up the status of a customer order by order ID.

    Args:
        order_id: The order ID to look up (e.g. ORD-1001).
    """
    order = db.get_order(order_id)
    if not order:
        return f"Order {order_id} not found. Please verify the order ID."
    return (
        f"Order {order_id}: {order['status']}. "
        f"Placed on {order['date']}, ETA: {order['eta']}."
    )


@function_tool
def check_return_policy(item_category: str) -> str:
    """Check the return policy for a given item category.

    Args:
        item_category: The category of the item (e.g. electronics, clothing, furniture).
    """
    policy = RETURN_POLICIES.get(item_category.lower())
    if not policy:
        return f"Sorry, no return policy found for '{item_category}'. Available categories: {', '.join(RETURN_POLICIES.keys())}."
    return f"Return policy for {item_category}: {policy}"


@function_tool
def check_ticket_status(ticket_id: str) -> str:
    """Check the status of a support ticket by ticket ID.

    Args:
        ticket_id: The ticket ID to look up (e.g. TKT-5001).
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return f"Ticket {ticket_id} not found. Please verify the ticket ID."
    return (
        f"Ticket {ticket_id}: {ticket['status']} (priority: {ticket['priority']}). "
        f"Issue: {ticket['issue']}."
    )


@function_tool
def escalate_to_human(customer_name: str, issue_description: str) -> str:
    """Escalate a customer issue to a human support agent.

    Args:
        customer_name: The name of the customer.
        issue_description: A description of the issue to escalate.
    """
    ticket_id = db.get_next_escalation_id()
    db.create_escalation(ticket_id, customer_name, issue_description)
    return (
        f"Your issue has been escalated. A human agent will follow up within 24 hours. "
        f"Your escalation ticket ID is {ticket_id}."
    )


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
)


def main():
    print("Support Desk Agent")
    print("=" * 50)

    if not _client:
        print("Error: No LLM configured. Set GEMINI_API_KEY or LLM_PROVIDER=ollama.")
        return

    db.init_db()

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        print(f"\nYou: {question}")
        try:
            result = Runner.run_sync(agent, question)
            print(f"\nAgent: {result.final_output}")
        except Exception as e:
            print(f"\nError: {e}")
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
                result = Runner.run_sync(agent, user_input)
                print(f"\nAgent: {result.final_output}\n")
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break


if __name__ == "__main__":
    main()
