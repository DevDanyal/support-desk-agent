import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "test_support_desk.db")
os.environ["GEMINI_API_KEY"] = "test-key"

import database as db


def setup_module():
    db.init_db()
    db.seed_demo_data()


def teardown_module():
    try:
        os.remove(os.environ["DB_PATH"])
    except OSError:
        pass


class TestOrders:
    def test_get_all_orders(self):
        orders = db.get_all_orders()
        assert len(orders) >= 5
        assert any(o["id"] == "ORD-1001" for o in orders)

    def test_get_order_found(self):
        order = db.get_order("ORD-1001")
        assert order is not None
        assert order["customer"] == "Alice Johnson"
        assert order["status"] == "shipped"

    def test_get_order_not_found(self):
        assert db.get_order("INVALID") is None

    def test_create_order(self):
        order = db.create_order("Test User", "Test Item", 99.99)
        assert order["customer"] == "Test User"
        assert order["items"] == "Test Item"
        assert order["total"] == 99.99
        assert order["status"] == "processing"
        db.delete_order(order["id"])

    def test_delete_order(self):
        order = db.create_order("Del User", "Del Item", 10.0)
        db.delete_order(order["id"])
        assert db.get_order(order["id"]) is None

    def test_order_count_by_status(self):
        counts = db.get_order_count_by_status()
        assert isinstance(counts, dict)
        assert sum(counts.values()) >= 5


class TestTickets:
    def test_get_all_tickets(self):
        tickets = db.get_all_tickets()
        assert len(tickets) >= 5

    def test_get_ticket_found(self):
        ticket = db.get_ticket("TKT-5001")
        assert ticket is not None
        assert ticket["issue"] == "Wrong item received"

    def test_get_ticket_not_found(self):
        assert db.get_ticket("INVALID") is None

    def test_create_ticket(self):
        ticket = db.create_ticket("Test User", "Test issue", "high")
        assert ticket["customer"] == "Test User"
        assert ticket["priority"] == "high"
        assert ticket["status"] == "open"

    def test_update_ticket_status(self):
        ticket = db.create_ticket("Update User", "Update issue")
        updated = db.update_ticket(ticket["id"], status="resolved")
        assert updated["status"] == "resolved"

    def test_update_ticket_assign(self):
        ticket = db.create_ticket("Assign User", "Assign issue")
        updated = db.update_ticket(ticket["id"], status="in_progress", assigned_to="Agent X")
        assert updated["assigned_to"] == "Agent X"
        assert updated["status"] == "in_progress"

    def test_ticket_count_by_status(self):
        counts = db.get_ticket_count_by_status()
        assert isinstance(counts, dict)

    def test_ticket_count_by_priority(self):
        counts = db.get_ticket_count_by_priority()
        assert isinstance(counts, dict)


class TestEscalations:
    def test_create_and_get(self):
        db.create_escalation("TKT-TEST-1", "Test User", "Test issue")
        escs = db.get_all_escalations()
        assert any(e["ticket_id"] == "TKT-TEST-1" for e in escs)

    def test_resolve(self):
        db.create_escalation("TKT-TEST-2", "User", "Issue")
        db.resolve_escalation("TKT-TEST-2")
        escs = db.get_all_escalations()
        resolved = [e for e in escs if e["ticket_id"] == "TKT-TEST-2"]
        assert len(resolved) == 1
        assert resolved[0]["status"] == "resolved"

    def test_pending_count(self):
        count = db.get_pending_escalation_count()
        assert isinstance(count, int)

    def test_next_id(self):
        esc_id = db.get_next_escalation_id()
        assert esc_id.startswith("TKT-")


class TestCustomers:
    def test_get_all_customers(self):
        customers = db.get_all_customers()
        assert len(customers) >= 5
        assert any(c["name"] == "Alice Johnson" for c in customers)

    def test_customer_count(self):
        count = db.get_customer_count()
        assert count >= 5


class TestChatHistory:
    def test_add_and_retrieve(self):
        conv_id = "test-conv-1"
        db.add_chat_message(conv_id, "user", "Hello")
        db.add_chat_message(conv_id, "assistant", "Hi there!")
        history = db.get_recent_chat_history(10)
        msgs = [m for m in history if m["conversation_id"] == conv_id]
        assert len(msgs) == 2
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["content"] == "Hi there!"


class TestStats:
    def test_get_stats(self):
        stats = db.get_stats()
        assert "total_orders" in stats
        assert "total_tickets" in stats
        assert "total_customers" in stats
        assert "pending_escalations" in stats
        assert "revenue" in stats
        assert "order_statuses" in stats
        assert "ticket_statuses" in stats
        assert "priority_counts" in stats
        assert stats["total_orders"] >= 5
        assert stats["total_customers"] >= 5
