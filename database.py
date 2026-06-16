import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_DEFAULT_DB = os.path.join(os.sep, "tmp", "support_desk.db") if os.environ.get("VERCEL") else "support_desk.db"
DB_PATH = os.getenv("DB_PATH", _DEFAULT_DB)


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

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            hashed_password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()

    # Seed default admin if no users exist
    row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    if row["c"] == 0:
        now = datetime.now().isoformat()
        admin_id = "admin-00000000-0000-0000-0000-000000000000"
        hashed = pwd_context.hash("admin123")
        conn.execute(
            "INSERT INTO users (id, email, username, hashed_password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, "admin@support.com", "Admin", hashed, "admin", now),
        )
        conn.commit()

    conn.close()


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


def load_conversation_state(conversation_id: str) -> Optional[str]:
    conn = get_conn()
    row = conn.execute(
        "SELECT state_json FROM conversation_state WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row["state_json"] if row else None


# ── Users / Auth ──

def create_user(email: str, username: str, password: str) -> dict:
    from uuid import uuid4
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return None
    user_id = str(uuid4())
    hashed = pwd_context.hash(password)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO users (id, email, username, hashed_password, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, email, username, hashed, "admin", now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row)


def get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


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
