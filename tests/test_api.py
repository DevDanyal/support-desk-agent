import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "test_api_support_desk.db")
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["LLM_PROVIDER"] = "gemini"

from unittest.mock import patch

import pytest
pytestmark = pytest.mark.asyncio
from httpx import AsyncClient, ASGITransport
from app import app


@pytest.fixture(autouse=True)
def setup_db():
    from database import init_db, get_conn
    conn = get_conn()
    conn.executescript("""
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS tickets;
        DROP TABLE IF EXISTS escalations;
        DROP TABLE IF EXISTS customers;
        DROP TABLE IF EXISTS chat_history;
    """)
    conn.close()
    init_db()
    yield
    try:
        os.remove(os.environ["DB_PATH"])
    except OSError:
        pass


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_get_stats(client):
    r = await client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "total_orders" in data
    assert "total_tickets" in data
    assert "total_customers" in data


@pytest.mark.asyncio
async def test_get_orders(client):
    r = await client.get("/api/orders")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 5


@pytest.mark.asyncio
async def test_get_order_found(client):
    r = await client.get("/api/orders/ORD-1001")
    assert r.status_code == 200
    data = r.json()
    assert data["customer"] == "Alice Johnson"


@pytest.mark.asyncio
async def test_get_order_not_found(client):
    r = await client.get("/api/orders/INVALID")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_order(client):
    r = await client.post("/api/orders", json={"customer": "Test", "items": "Item", "total": 10.0})
    assert r.status_code == 200
    data = r.json()
    assert data["customer"] == "Test"
    assert data["total"] == 10.0


@pytest.mark.asyncio
async def test_delete_order(client):
    r = await client.post("/api/orders", json={"customer": "Del", "items": "X", "total": 1.0})
    order_id = r.json()["id"]
    r = await client.delete(f"/api/orders/{order_id}")
    assert r.status_code == 200
    r = await client.get(f"/api/orders/{order_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_tickets(client):
    r = await client.get("/api/tickets")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 5


@pytest.mark.asyncio
async def test_get_ticket_found(client):
    r = await client.get("/api/tickets/TKT-5001")
    assert r.status_code == 200
    data = r.json()
    assert data["issue"] == "Wrong item received"


@pytest.mark.asyncio
async def test_create_ticket(client):
    r = await client.post("/api/tickets", json={"customer": "Test", "issue": "Bug", "priority": "high"})
    assert r.status_code == 200
    data = r.json()
    assert data["priority"] == "high"


@pytest.mark.asyncio
async def test_update_ticket(client):
    r = await client.post("/api/tickets", json={"customer": "Test", "issue": "Fix"})
    ticket_id = r.json()["id"]
    r = await client.patch(f"/api/tickets/{ticket_id}", json={"status": "resolved"})
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"


@pytest.mark.asyncio
async def test_get_ticket_not_found(client):
    r = await client.get("/api/tickets/INVALID")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_escalations(client):
    r = await client.get("/api/escalations")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_resolve_escalation(client):
    r = await client.post("/api/escalations/TKT-5004/resolve")
    assert r.status_code == 200
    r = await client.get("/api/escalations")
    escs = r.json()
    resolved = [e for e in escs if e["ticket_id"] == "TKT-5004"]
    assert resolved[0]["status"] == "resolved"


@pytest.mark.asyncio
async def test_get_customers(client):
    r = await client.get("/api/customers")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 5


@pytest.mark.asyncio
async def test_get_return_policies(client):
    r = await client.get("/api/return-policies")
    assert r.status_code == 200
    data = r.json()
    assert "electronics" in data
    assert "clothing" in data
    assert "furniture" in data


@pytest.mark.asyncio
async def test_chat_endpoint(client):
    with patch("app.run_agent") as mock_run:
        mock_run.return_value = {"reply": "Hello! How can I help?", "conversation_id": "mock-cid", "tool_calls": []}
        r = await client.post("/api/chat", json={"message": "Hello"})
    assert r.status_code == 200
    data = r.json()
    assert "reply" in data
    assert "conversation_id" in data
    assert data["reply"] == "Hello! How can I help?"


@pytest.mark.asyncio
async def test_chat_history(client):
    r = await client.get("/api/chat-history")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_chat_with_conversation_id(client):
    with patch("app.run_agent") as mock_run:
        mock_run.return_value = {"reply": "Reply", "conversation_id": "test-conv-ccc", "tool_calls": []}
        r = await client.post("/api/chat", json={"message": "First", "conversation_id": "test-conv-ccc"})
        assert r.status_code == 200
        assert r.json()["conversation_id"] == "test-conv-ccc"
        assert r.json()["reply"] == "Reply"
        mock_run.return_value = {"reply": "Second reply", "conversation_id": "test-conv-ccc", "tool_calls": []}
        r = await client.post("/api/chat", json={"message": "Second", "conversation_id": "test-conv-ccc"})
        assert r.status_code == 200
        assert r.json()["conversation_id"] == "test-conv-ccc"

