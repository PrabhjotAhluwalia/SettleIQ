"""Internal shared SQLite helper for skills."""
from __future__ import annotations

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.abspath(os.path.join(HERE, "..", "data", "settleiq.db"))

# Expected tables — used for the readiness check
_REQUIRED_TABLES = frozenset({
    "merchant_registry",
    "settlement_events",
    "pipeline_logs",
    "bank_downtime_events",
    "chargebacks",
})


def _db_is_ready(path: str) -> bool:
    """Return True if the DB file exists and has all required tables."""
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path)
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


def connect() -> sqlite3.Connection:
    """Open a connection to the SettleIQ database.

    Raises a clear RuntimeError with setup instructions if the database has
    not been seeded yet (i.e. ``python data/mock_generator.py`` was not run).
    """
    if not _db_is_ready(DB_PATH):
        raise RuntimeError(
            f"SettleIQ database not found or incomplete: {DB_PATH}\n"
            "Run the data generator first:\n"
            "    python data/mock_generator.py"
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
