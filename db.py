"""SQLite storage. One table of distress 'leads', deduplicated by a stable key."""
import hashlib
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("RADAR_DB", "radar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            TEXT PRIMARY KEY,          -- hash of source + source_ref
    kind          TEXT NOT NULL,             -- insolvency | auction | mortgagee_sale
    status        TEXT,                      -- e.g. liquidation, administration, receivership
    entity_name   TEXT,
    abn           TEXT,
    acn           TEXT,
    state         TEXT,                      -- NSW VIC QLD WA SA TAS ACT NT
    suburb        TEXT,
    postcode      TEXT,
    asset_summary TEXT,
    notice_date   TEXT,                      -- ISO date
    deadline      TEXT,                      -- meeting / auction / claim deadline
    contact_name  TEXT,
    contact_firm  TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    source        TEXT NOT NULL,
    source_url    TEXT,
    raw_text      TEXT,
    confidence    REAL DEFAULT 0.5,
    first_seen    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads(kind);
CREATE INDEX IF NOT EXISTS idx_leads_date ON leads(notice_date);
"""


@contextmanager
def connect(path: str | None = None):
    target = path or DB_PATH
    if os.path.dirname(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def lead_id(source: str, source_ref: str) -> str:
    return hashlib.sha1(f"{source}|{source_ref}".encode()).hexdigest()[:16]


COLUMNS = [
    "id", "kind", "status", "entity_name", "abn", "acn", "state", "suburb",
    "postcode", "asset_summary", "notice_date", "deadline", "contact_name",
    "contact_firm", "contact_phone", "contact_email", "source", "source_url",
    "raw_text", "confidence",
]


def upsert(con, lead: dict) -> None:
    row = {c: lead.get(c) for c in COLUMNS}
    placeholders = ",".join("?" for _ in COLUMNS)
    updates = ",".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "id")
    con.execute(
        f"INSERT INTO leads ({','.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [row[c] for c in COLUMNS],
    )


def search(con, kind=None, state=None, status=None, q=None, limit=200):
    sql, args = "SELECT * FROM leads WHERE 1=1", []
    if kind:
        sql += " AND kind=?"; args.append(kind)
    if state:
        sql += " AND state=?"; args.append(state.upper())
    if status:
        sql += " AND status=?"; args.append(status)
    if q:
        sql += " AND (entity_name LIKE ? OR asset_summary LIKE ? OR suburb LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY notice_date DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args)]
