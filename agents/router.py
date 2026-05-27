"""Agent 2 - MCP-style query router.

Responsibilities:
  - Resolve relative dates to ISO timestamps in America/New_York (ET).
  - Fuzzy-match merchant names -> MID.
  - Resolve phone numbers -> list of MIDs (UC3 disambiguation).
  - Identify payout IDs, transaction IDs, return codes.
  - Pick the right domain skill from DOMAIN_REGISTRY.json.
  - Output a structured JSON envelope passed downstream.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from difflib import get_close_matches
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(HERE, "..", "data"))
DB_PATH = os.path.join(DATA_DIR, "settleiq.db")
REGISTRY_PATH = os.path.join(DATA_DIR, "DOMAIN_REGISTRY.json")

# Required tables — mirrors skills/_db.py so the router gives the same
# helpful error message on a fresh clone before seeding.
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

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Date parsing (ET)
# ---------------------------------------------------------------------------

REL_DATE_PATTERNS = [
    (re.compile(r"\btoday\b"), 0),
    (re.compile(r"\byesterday\b"), 1),
    (re.compile(r"\blast (\d+) days?\b"), None),
    (re.compile(r"\bpast (\d+) days?\b"), None),
    (re.compile(r"\blast week\b"), 7),
    (re.compile(r"\blast month\b"), 30),
    (re.compile(r"\blast 30 days?\b"), 30),
    (re.compile(r"\blast 7 days?\b"), 7),
    (re.compile(r"\blast 24 ?h(ours?)?\b"), 1),
]

ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:to|-|–|through)\s*(\d{4}-\d{2}-\d{2})")
MID_RE = re.compile(r"\bMID\d{5}\b", re.IGNORECASE)
PAYOUT_RE = re.compile(r"\bPO[_-]?\d{4,8}\b", re.IGNORECASE)
TXN_RE = re.compile(r"\bTXN[_-]?\d{6,10}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
RETURN_CODE_RE = re.compile(r"\b(R0[1-9]|R1[0-9]|BANK_TIMEOUT|RAIL_REJECT)\b", re.IGNORECASE)


def now_et() -> datetime:
    return datetime.now(ET)


def resolve_date_range(query: str) -> dict:
    """Return {'start': iso, 'end': iso, 'description': str} or {} if none found."""
    q = query.lower()
    today = now_et().replace(hour=0, minute=0, second=0, microsecond=0)

    # Explicit ISO range
    m = DATE_RANGE_RE.search(query)
    if m:
        start = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(m.group(2), "%Y-%m-%d").replace(tzinfo=ET, hour=23, minute=59, second=59)
        return {"start": start.isoformat(), "end": end.isoformat(), "description": f"{m.group(1)} to {m.group(2)}"}

    # Single ISO date -> that whole day
    m = ISO_DATE_RE.search(query)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=ET)
        return {
            "start": d.isoformat(),
            "end": d.replace(hour=23, minute=59, second=59).isoformat(),
            "description": m.group(1),
        }

    # Relative
    if "today" in q:
        return {"start": today.isoformat(), "end": (today + timedelta(days=1, seconds=-1)).isoformat(),
                "description": "today (ET)"}
    if "yesterday" in q:
        y = today - timedelta(days=1)
        return {"start": y.isoformat(), "end": (today - timedelta(seconds=1)).isoformat(),
                "description": "yesterday (ET)"}

    m = re.search(r"last (\d+) days?", q) or re.search(r"past (\d+) days?", q)
    if m:
        n = int(m.group(1))
        start = today - timedelta(days=n)
        return {"start": start.isoformat(), "end": now_et().isoformat(),
                "description": f"last {n} days (ET)"}

    if "last week" in q:
        return {"start": (today - timedelta(days=7)).isoformat(), "end": now_et().isoformat(),
                "description": "last 7 days (ET)"}
    if "last month" in q or "last 30 days" in q:
        return {"start": (today - timedelta(days=30)).isoformat(), "end": now_et().isoformat(),
                "description": "last 30 days (ET)"}
    if "last 7 days" in q:
        return {"start": (today - timedelta(days=7)).isoformat(), "end": now_et().isoformat(),
                "description": "last 7 days (ET)"}
    if "last 24" in q:
        return {"start": (now_et() - timedelta(hours=24)).isoformat(), "end": now_et().isoformat(),
                "description": "last 24 hours (ET)"}

    return {}


# ---------------------------------------------------------------------------
# Merchant matching
# ---------------------------------------------------------------------------

def _load_merchant_lookup() -> tuple[dict, dict, list[str]]:
    """Build name->mid, phone->[mids], list of names."""
    conn = _checked_connect()
    rows = conn.execute(
        "SELECT mid, business_name, phone_number FROM merchant_registry"
    ).fetchall()
    conn.close()
    name_to_mid: dict[str, str] = {}
    phone_to_mids: dict[str, list[str]] = {}
    names: list[str] = []
    for mid, name, phone in rows:
        name_to_mid[name.lower()] = mid
        names.append(name)
        phone_to_mids.setdefault(phone, []).append(mid)
    return name_to_mid, phone_to_mids, names


def fuzzy_match_merchant(query: str) -> dict:
    """Return {'mid': str, 'name': str, 'method': str} or {}."""
    # Direct MID
    m = MID_RE.search(query)
    if m:
        mid = m.group(0).upper()
        conn = _checked_connect()
        row = conn.execute(
            "SELECT business_name FROM merchant_registry WHERE mid = ?", (mid,)
        ).fetchone()
        conn.close()
        if row:
            return {"mid": mid, "name": row[0], "method": "explicit_mid"}

    name_to_mid, _, names = _load_merchant_lookup()

    lq = query.lower()
    # Exact substring
    for name in names:
        if name.lower() in lq:
            return {"mid": name_to_mid[name.lower()], "name": name, "method": "substring"}

    # Fuzzy
    candidates = get_close_matches(lq, [n.lower() for n in names], n=1, cutoff=0.6)
    if candidates:
        match = candidates[0]
        mid = name_to_mid[match]
        # find original casing
        orig = next(n for n in names if n.lower() == match)
        return {"mid": mid, "name": orig, "method": "fuzzy"}

    # Token-based: any 4+ char token matches a name token
    tokens = [t for t in re.split(r"[^a-z0-9]+", lq) if len(t) >= 4]
    for t in tokens:
        for name in names:
            if t in name.lower():
                return {"mid": name_to_mid[name.lower()], "name": name, "method": "token"}
    return {}


def resolve_phone(query: str) -> list[dict]:
    """Return list of {'mid','name'} for any phone match."""
    m = PHONE_RE.search(query)
    if not m:
        return []
    raw = m.group(0)
    # normalize digits
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11:
        return []
    canonical = f"+1-{digits[1:4]}-{digits[4:7]}-{digits[7:11]}"
    _, phone_to_mids, _ = _load_merchant_lookup()
    mids = phone_to_mids.get(canonical, [])
    if not mids:
        return []
    conn = _checked_connect()
    placeholders = ",".join("?" * len(mids))
    rows = conn.execute(
        f"SELECT mid, business_name FROM merchant_registry WHERE mid IN ({placeholders})",
        mids,
    ).fetchall()
    conn.close()
    return [{"mid": mid, "name": name, "phone": canonical} for mid, name in rows]


# ---------------------------------------------------------------------------
# Domain selection
# ---------------------------------------------------------------------------

DOMAIN_KEYWORDS = [
    ("chargeback_info", ["chargeback", "dispute", "reason code", "txn_", "transaction id"]),
    ("downtime_events", ["downtime", "outage", "rail down", "bank down", "ach down", "fedwire down", "rtp down"]),
    ("settlement_config", ["config", "schedule", "frequency", "t+0", "t+1", "cutoff", "rail timing", "ach timing"]),
    ("reserve_balance", ["reserve", "balance"]),
    ("trend_analysis", ["trend", "last 30 days", "rolling", "average settlement", "avg settlement", "failure rate"]),
    ("failure_analysis", ["failure", "failed", "return code", "r01", "r03", "r04", "diagnos", "why did", "why failed"]),
    ("settlement_status", ["settlement", "settled", "payout", "amount", "net", "trace", "provisional", "freeze", "frozen", "on hold", "status"]),
]


def pick_domain(query: str) -> str:
    lq = query.lower()
    for domain, kws in DOMAIN_KEYWORDS:
        if any(kw in lq for kw in kws):
            return domain
    # default
    return "settlement_status"


def load_registry() -> dict:
    with open(REGISTRY_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Main route()
# ---------------------------------------------------------------------------

@dataclass
class RouterEnvelope:
    domain: str
    skill_path: str
    skill_entry: str
    merchant: dict = field(default_factory=dict)
    phone_matches: list = field(default_factory=list)
    payout_id: str | None = None
    transaction_id: str | None = None
    return_code: str | None = None
    date_range: dict = field(default_factory=dict)
    intent_flags: dict = field(default_factory=dict)
    raw_query: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def route(query: str) -> RouterEnvelope:
    registry = load_registry()

    # entity extraction
    merchant = fuzzy_match_merchant(query)
    phones = resolve_phone(query)
    payout = PAYOUT_RE.search(query)
    txn = TXN_RE.search(query)
    rc = RETURN_CODE_RE.search(query)
    date_range = resolve_date_range(query)

    # detect special intents
    lq = query.lower()
    flags = {
        "freeze_check": any(w in lq for w in ("freeze", "frozen", "on hold", "on-hold", "hold status")),
        "provisional_check": "provisional" in lq,
        "disambiguation_needed": len(phones) > 1,
    }

    # Override domain choice based on extracted entities
    if txn or "chargeback" in lq or "dispute" in lq:
        domain = "chargeback_info"
    elif rc or "return code" in lq or "why did" in lq or "diagnos" in lq:
        domain = "failure_analysis"
    elif payout:
        domain = "settlement_status"
    elif flags["freeze_check"]:
        domain = "settlement_status"
    else:
        domain = pick_domain(query)

    entry = registry[domain]
    return RouterEnvelope(
        domain=domain,
        skill_path=entry["skill_path"],
        skill_entry=entry["entry"],
        merchant=merchant,
        phone_matches=phones,
        payout_id=payout.group(0).upper().replace("-", "_") if payout else None,
        transaction_id=txn.group(0).upper().replace("-", "_") if txn else None,
        return_code=rc.group(0).upper() if rc else None,
        date_range=date_range,
        intent_flags=flags,
        raw_query=query,
    )
