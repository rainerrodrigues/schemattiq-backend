"""SQLite log of every design session and HITL approval (proof for the demo).

On Render, point DB_PATH at a persistent disk (e.g. /var/data/schemattiq.db),
otherwise the file is wiped on every deploy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "schemattiq.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    client_id TEXT,
    idea TEXT NOT NULL,
    budget_inr INTEGER NOT NULL,
    status TEXT NOT NULL,            -- running | completed | failed
    grantex_posture TEXT,
    total_inr REAL,
    sandbox_verified INTEGER,
    result_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS hitl (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT,
    title TEXT NOT NULL,
    total_inr REAL NOT NULL,
    budget_inr REAL NOT NULL,
    phone_last4 TEXT,
    status TEXT NOT NULL,            -- HITL_REQUIRED | CALLING | APPROVED | REJECTED | CALL_FAILED | NO_ANSWER
    call_uuid TEXT,
    detail TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    client_id TEXT,
    source TEXT,                     -- ?src= / utm_source tag, else referrer host, else "direct"
    device TEXT,                     -- mobile | desktop
    ip_hash TEXT                     -- salted one-way hash; raw IPs are never stored
);
CREATE INDEX IF NOT EXISTS visits_created ON visits(created_at);
CREATE TABLE IF NOT EXISTS payments (
    payment_link_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    amount_inr REAL NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _db() as c:
        c.executescript(SCHEMA)


def start_session(idea: str, budget_inr: int, client_id: str | None) -> str:
    sid = uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("INSERT INTO sessions (id, created_at, client_id, idea, budget_inr, status) "
                  "VALUES (?, ?, ?, ?, ?, 'running')", (sid, _now(), client_id, idea, budget_inr))
    return sid


def finish_session(sid: str, result: dict | None, error: str | None = None) -> None:
    result = result or {}
    with _db() as c:
        c.execute(
            "UPDATE sessions SET status=?, grantex_posture=?, total_inr=?, sandbox_verified=?, "
            "result_json=?, error=? WHERE id=?",
            ("failed" if error else "completed", result.get("grantex_posture"), result.get("total_inr"),
             int(bool((result.get("sandbox_verification") or {}).get("verified"))),
             json.dumps(result) if result else None, error, sid),
        )


def get_session_result(sid: str) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT result_json FROM sessions WHERE id=?", (sid,)).fetchone()
    return json.loads(row["result_json"]) if row and row["result_json"] else None


def stats() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    with _db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS today, "
            "SUM(CASE WHEN created_at >= ? AND sandbox_verified = 1 THEN 1 ELSE 0 END) AS verified_today, "
            "COUNT(DISTINCT client_id) AS unique_clients "
            "FROM sessions WHERE status = 'completed'", (today, today)).fetchone()
    return {k: row[k] or 0 for k in ("total", "today", "verified_today", "unique_clients")}


def recent_sessions(limit: int = 50) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT id, created_at, idea, budget_inr, status, grantex_posture, total_inr, sandbox_verified "
            "FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def create_hitl(title: str, total_inr: float, budget_inr: float, phone: str | None,
                session_id: str | None = None) -> str:
    hid = uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute(
            "INSERT INTO hitl (id, created_at, session_id, title, total_inr, budget_inr, phone_last4, status, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'HITL_REQUIRED', ?)",
            (hid, _now(), session_id, title, total_inr, budget_inr, (phone or "")[-4:] or None, _now()))
    return hid


def update_hitl(hid: str, status: str, call_uuid: str | None = None, detail: str | None = None) -> None:
    with _db() as c:
        c.execute("UPDATE hitl SET status=?, call_uuid=COALESCE(?, call_uuid), detail=COALESCE(?, detail), "
                  "updated_at=? WHERE id=?", (status, call_uuid, detail, _now(), hid))


def log_visit(client_id: str | None, source: str, device: str, ip_hash: str | None) -> None:
    with _db() as c:
        c.execute("INSERT INTO visits (created_at, client_id, source, device, ip_hash) VALUES (?, ?, ?, ?, ?)",
                  (_now(), client_id, source, device, ip_hash))


def usage() -> dict:
    """Everything the /admin dashboard shows."""
    today = datetime.now(timezone.utc).date().isoformat()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    # A visitor is a browser ID, or the IP hash for browsers that block storage.
    who = "COALESCE(client_id, ip_hash)"
    with _db() as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        out = {
            "visitors_total": one(f"SELECT COUNT(DISTINCT {who}) FROM visits"),
            "visitors_today": one(f"SELECT COUNT(DISTINCT {who}) FROM visits WHERE created_at >= ?", today),
            "page_views_total": one("SELECT COUNT(*) FROM visits"),
            "page_views_today": one("SELECT COUNT(*) FROM visits WHERE created_at >= ?", today),
            "designs_total": one("SELECT COUNT(*) FROM sessions"),
            "designs_today": one("SELECT COUNT(*) FROM sessions WHERE created_at >= ?", today),
            "designers_total": one("SELECT COUNT(DISTINCT client_id) FROM sessions"),
            "designs_failed": one("SELECT COUNT(*) FROM sessions WHERE status = 'failed'"),
            "approval_calls": one("SELECT COUNT(*) FROM hitl WHERE status != 'HITL_REQUIRED'"),
            "payments": one("SELECT COUNT(*) FROM payments"),
            "first_visit_at": c.execute("SELECT MIN(created_at) FROM visits").fetchone()[0],
        }
        out["sources"] = [dict(r) for r in c.execute(
            f"SELECT source, COUNT(DISTINCT {who}) AS visitors, COUNT(*) AS views FROM visits "
            "GROUP BY source ORDER BY visitors DESC LIMIT 15")]
        out["devices"] = [dict(r) for r in c.execute(
            f"SELECT device, COUNT(DISTINCT {who}) AS visitors FROM visits GROUP BY device ORDER BY visitors DESC")]
        out["hourly"] = [dict(r) for r in c.execute(
            f"SELECT substr(created_at, 1, 13) AS hour, COUNT(DISTINCT {who}) AS visitors, COUNT(*) AS views "
            "FROM visits WHERE created_at >= ? GROUP BY hour ORDER BY hour", (since,))]
        out["postures"] = [dict(r) for r in c.execute(
            "SELECT COALESCE(grantex_posture, status) AS posture, COUNT(*) AS n FROM sessions "
            "GROUP BY posture ORDER BY n DESC")]
    out["recent_designs"] = recent_sessions(25)
    return out


def save_payment(link_id: str, session_id: str, amount_inr: float, url: str, status: str) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO payments (payment_link_id, created_at, session_id, amount_inr, url, "
                  "status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (link_id, _now(), session_id, amount_inr, url, status, _now()))


def update_payment_status(link_id: str, status: str) -> None:
    with _db() as c:
        c.execute("UPDATE payments SET status=?, updated_at=? WHERE payment_link_id=?", (status, _now(), link_id))


def get_payment(link_id: str) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM payments WHERE payment_link_id=?", (link_id,)).fetchone()
    return dict(row) if row else None


def payment_for_session(session_id: str) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM payments WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                        (session_id,)).fetchone()
    return dict(row) if row else None


def hitl_approved_for_session(session_id: str) -> bool:
    with _db() as c:
        row = c.execute("SELECT 1 FROM hitl WHERE session_id=? AND status='APPROVED' LIMIT 1", (session_id,)).fetchone()
    return row is not None


def get_hitl(hid: str) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM hitl WHERE id=?", (hid,)).fetchone()
    return dict(row) if row else None
