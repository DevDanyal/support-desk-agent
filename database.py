import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "support_desk.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'processing',
            date TEXT NOT NULL,
            eta TEXT NOT NULL,
            customer TEXT NOT NULL,
            items TEXT NOT NULL,
            total REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'medium',
            issue TEXT NOT NULL,
            customer TEXT NOT NULL,
            date TEXT NOT NULL,
            assigned_to TEXT NOT NULL DEFAULT 'Unassigned'
        );

        CREATE TABLE IF NOT EXISTS escalations (
            ticket_id TEXT PRIMARY KEY,
            customer TEXT NOT NULL,
            issue TEXT NOT NULL,
            escalated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            orders INTEGER NOT NULL DEFAULT 0,
            tickets INTEGER NOT NULL DEFAULT 0,
            member_since TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS conversation_state (
            conversation_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # Seed data if empty
    row = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()
    if row["c"] == 0:
        _seed(conn)
    conn.close()


def _seed(conn):
    now = datetime.now()
    orders = [
        ("ORD-1001", "shipped", "2026-06-01", "2026-06-10", "Alice Johnson", "Wireless Headphones", 89.99),
        ("ORD-1002", "processing", "2026-06-05", "2026-06-15", "Bob Smith", "USB-C Hub, Mouse Pad", 34.50),
        ("ORD-1003", "delivered", "2026-05-20", "2026-05-28", "Carol Davis", "Mechanical Keyboard", 149.99),
        ("ORD-1004", "cancelled", "2026-05-25", "N/A", "David Wilson", "Monitor Stand", 45.00),
        ("ORD-1005", "processing", "2026-06-07", "2026-06-17", "Eve Martinez", "Webcam, Microphone", 129.99),
    ]
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)

    tickets = [
        ("TKT-5001", "open", "high", "Wrong item received", "Alice Johnson", "2026-06-02", "Unassigned"),
        ("TKT-5002", "in_progress", "medium", "Refund not processed", "Bob Smith", "2026-06-06", "Sarah Chen"),
        ("TKT-5003", "resolved", "low", "Shipping address change", "Carol Davis", "2026-05-22", "Mike Ross"),
        ("TKT-5004", "open", "urgent", "Account hacked - unauthorized purchases", "Frank Lee", "2026-06-08", "Unassigned"),
        ("TKT-5005", "open", "medium", "Damaged product on delivery", "Grace Kim", "2026-06-09", "Unassigned"),
    ]
    conn.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?)", tickets)

    escalations = [
        ("TKT-5004", "Frank Lee", "Account hacked - unauthorized purchases", "2026-06-08 14:30", "pending"),
    ]
    conn.executemany("INSERT INTO escalations VALUES (?,?,?,?,?)", escalations)

    customers = [
        ("CST-001", "Alice Johnson", "alice@email.com", 3, 1, "2025-03-15"),
        ("CST-002", "Bob Smith", "bob@email.com", 1, 1, "2025-06-20"),
        ("CST-003", "Carol Davis", "carol@email.com", 5, 1, "2024-11-01"),
        ("CST-004", "David Wilson", "david@email.com", 2, 0, "2025-08-12"),
        ("CST-005", "Eve Martinez", "eve@email.com", 4, 0, "2025-01-05"),
    ]
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)
    conn.commit()


# ── Orders ──

def get_all_orders():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM orders").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_order(order_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_order(customer: str, items: str, total: float):
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
    order_id = f"ORD-{count + 2000}"
    today = datetime.now().strftime("%Y-%m-%d")
    eta = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
        (order_id, "processing", today, eta, customer, items, total),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(row)


def delete_order(order_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()


def get_order_count_by_status():
    conn = get_conn()
    rows = conn.execute("SELECT status, COUNT(*) as c FROM orders GROUP BY status").fetchall()
    conn.close()
    return {r["status"]: r["c"] for r in rows}


# ── Tickets ──

def get_all_tickets():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tickets").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ticket(ticket_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_ticket(customer: str, issue: str, priority: str = "medium"):
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM tickets").fetchone()["c"]
    ticket_id = f"TKT-{count + 6000}"
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO tickets VALUES (?,?,?,?,?,?,?)",
        (ticket_id, "open", priority, issue, customer, today, "Unassigned"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row)


def update_ticket(ticket_id: str, status: str, assigned_to: str = None):
    conn = get_conn()
    if assigned_to:
        conn.execute("UPDATE tickets SET status = ?, assigned_to = ? WHERE id = ?",
                     (status, assigned_to, ticket_id))
    else:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
    conn.commit()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_ticket_count_by_status():
    conn = get_conn()
    rows = conn.execute("SELECT status, COUNT(*) as c FROM tickets GROUP BY status").fetchall()
    conn.close()
    return {r["status"]: r["c"] for r in rows}


def get_ticket_count_by_priority():
    conn = get_conn()
    rows = conn.execute("SELECT priority, COUNT(*) as c FROM tickets GROUP BY priority").fetchall()
    conn.close()
    return {r["priority"]: r["c"] for r in rows}


# ── Escalations ──

def get_all_escalations():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM escalations").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_escalation(ticket_id: str, customer: str, issue: str):
    conn = get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO escalations VALUES (?,?,?,?,?)",
        (ticket_id, customer, issue, now, "pending"),
    )
    conn.commit()
    conn.close()


def resolve_escalation(ticket_id: str):
    conn = get_conn()
    conn.execute("UPDATE escalations SET status = 'resolved' WHERE ticket_id = ?", (ticket_id,))
    conn.commit()
    conn.close()


def get_pending_escalation_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as c FROM escalations WHERE status = 'pending'").fetchone()
    conn.close()
    return row["c"]


def get_next_escalation_id():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM escalations").fetchone()["c"]
    conn.close()
    return f"TKT-{count + 6000}"


# ── Customers ──

def get_all_customers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM customers").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as c FROM customers").fetchone()
    conn.close()
    return row["c"]


# ── Chat History ──

def add_chat_message(conversation_id: str, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_history (conversation_id, role, content) VALUES (?, ?, ?)",
        (conversation_id, role, content),
    )
    conn.commit()
    conn.close()


def get_recent_chat_history(limit: int = 50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def get_conversation_history(conversation_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_conversation_state(conversation_id: str, state_json: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO conversation_state (conversation_id, state_json, updated_at) VALUES (?, ?, datetime('now'))",
        (conversation_id, state_json),
    )
    conn.commit()
    conn.close()


def load_conversation_state(conversation_id: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT state_json FROM conversation_state WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row["state_json"] if row else None


# ── Stats ──

def get_stats():
    conn = get_conn()
    total_orders = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
    total_tickets = conn.execute("SELECT COUNT(*) as c FROM tickets").fetchone()["c"]
    total_customers = conn.execute("SELECT COUNT(*) as c FROM customers").fetchone()["c"]
    pending_esc = conn.execute("SELECT COUNT(*) as c FROM escalations WHERE status = 'pending'").fetchone()["c"]
    revenue = conn.execute("SELECT COALESCE(SUM(total), 0) as r FROM orders").fetchone()["r"]
    conn.close()

    order_statuses = get_order_count_by_status()
    ticket_statuses = get_ticket_count_by_status()
    priority_counts = get_ticket_count_by_priority()

    return {
        "total_orders": total_orders,
        "total_tickets": total_tickets,
        "total_customers": total_customers,
        "pending_escalations": pending_esc,
        "order_statuses": order_statuses,
        "ticket_statuses": ticket_statuses,
        "priority_counts": priority_counts,
        "revenue": revenue,
    }
