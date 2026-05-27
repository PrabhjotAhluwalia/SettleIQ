"""Agent 6 - Auditor / logger.

Persists a full trace of every pipeline run into the pipeline_logs table
of the local SQLite database. Mock token cost is calculated from the
character count of the response (no real API calls).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.abspath(os.path.join(HERE, "..", "data", "settleiq.db"))

_REQUIRED_TABLES = frozenset({
    "merchant_registry",
    "settlement_events",
    "pipeline_logs",
    "bank_downtime_events",
    "chargebacks",
})


def _db_is_ready() -> bool:
    """Return True if the DB file exists and has all required tables."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        return _REQUIRED_TABLES.issubset(tables)
    except Exception:
        return False


def _checked_connect() -> sqlite3.Connection:
    """Open the DB with a clear error if the DB hasn't been seeded."""
    if not _db_is_ready():
        raise RuntimeError(
            f"SettleIQ database not found or incomplete: {DB_PATH}\n"
            "Run the data generator first:\n"
            "    python data/mock_generator.py"
        )
    return sqlite3.connect(DB_PATH)

# Pricing: mock cents per 1k chars. Not real - just for the dashboard.
MOCK_RATE_PER_1K_CHARS = 0.0008


def mock_token_cost(text: str) -> float:
    return round((len(text or "") / 1000.0) * MOCK_RATE_PER_1K_CHARS, 6)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def log_trace(
    *,
    user_email: str,
    user_role: str,
    query: str,
    classifier_result: dict,
    router_result: dict | None,
    analyst_result: dict | None,
    validator_result: dict | None,
    final_response: str,
    latency_ms: int,
) -> dict:
    """Insert a row in pipeline_logs and return the persisted record."""
    conn = _checked_connect()
    cur = conn.cursor()
    cost = mock_token_cost(final_response)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_email": user_email,
        "user_role": user_role,
        "query": query,
        "classifier_result": json.dumps(classifier_result),
        "router_result": json.dumps(router_result, default=str) if router_result else None,
        "analyst_result": json.dumps(analyst_result, default=str) if analyst_result else None,
        "validator_result": json.dumps(validator_result, default=str) if validator_result else None,
        "final_response": final_response,
        "latency_ms": latency_ms,
        "token_cost_usd": cost,
        "sanity_status": (validator_result or {}).get("status", "n/a"),
        "response_hash": _hash(final_response),
    }
    cur.execute(
        """INSERT INTO pipeline_logs (
            ts,user_email,user_role,query,classifier_result,router_result,
            analyst_result,validator_result,final_response,latency_ms,
            token_cost_usd,sanity_status,response_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rec["ts"], rec["user_email"], rec["user_role"], rec["query"],
            rec["classifier_result"], rec["router_result"], rec["analyst_result"],
            rec["validator_result"], rec["final_response"], rec["latency_ms"],
            rec["token_cost_usd"], rec["sanity_status"], rec["response_hash"],
        ),
    )
    conn.commit()
    conn.close()
    return rec


def recent_logs(limit: int = 10) -> list[dict]:
    conn = _checked_connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, user_email, user_role, query, sanity_status, latency_ms, "
        "token_cost_usd, response_hash FROM pipeline_logs "
        "ORDER BY log_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
