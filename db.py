
"""SQLite storage for Insolvency Radar.
One table of distress leads, deduplicated by a stable key.
"""

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("RADAR_DB", "radar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT,
    entity_name   TEXT,
    abn           TEXT,
    acn           TEXT,
    state         TEXT,
    suburb        TEXT,
    postcode      TEXT,
    asset_summary TEXT,
    notice_date   TEXT,
    deadline      TEXT,
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

CREATE INDEX IF NOT EXISTS idx_leads_state
ON leads(state);

CREATE INDEX IF NOT EXISTS idx_leads_kind
ON leads(kind);

CREATE INDEX IF NOT EXISTS idx_leads_date
ON leads(notice_date);
"""


@contextmanager
def connect(path: str | None = None):
    target = str(path or DB_PATH)

    directory = os.path.dirname(target)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(
        target,
        timeout=30
    )

    try:
        con.row_factory = sqlite3.Row

        # Wait up to 30 seconds if another operation
        # temporarily locks the database.
        con.execute("PRAGMA busy_timeout=30000")

        # Enforce foreign key constraints if added later.
        con.execute("PRAGMA foreign_keys=ON")

        # Create tables and indexes if they do not exist.
        con.executescript(SCHEMA)

        yield con

        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


def lead_id(source: str, source_ref: str) -> str:
    value = f"{source}|{source_ref}"
    return hashlib.sha1(value.encode()).hexdigest()[:16]


COLUMNS = [
    "id",
    "kind",
    "status",
    "entity_name",
    "abn",
    "acn",
    "state",
    "suburb",
    "postcode",
    "asset_summary",
    "notice_date",
    "deadline",
    "contact_name",
    "contact_firm",
    "contact_phone",
    "contact_email",
    "source",
    "source_url",
    "raw_text",
    "confidence",
]


def upsert(con, lead: dict) -> None:
    row = {column: lead.get(column) for column in COLUMNS}

    placeholders = ",".join("?" for _ in COLUMNS)

    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in COLUMNS
        if column != "id"
    )

    con.execute(
        f"""
        INSERT INTO leads ({','.join(COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {updates}
        """,
        [row[column] for column in COLUMNS],
    )


def search(
    con,
    kind=None,
    state=None,
    status=None,
    q=None,
    limit=200
):
    sql = "SELECT * FROM leads WHERE 1=1"
    args = []

    if kind:
        sql += " AND kind=?"
        args.append(kind)

    if state:
        sql += " AND state=?"
        args.append(state.upper())

    if status:
        sql += " AND status=?"
        args.append(status)

    if q:
        sql += """
        AND (
            entity_name LIKE ?
            OR asset_summary LIKE ?
            OR suburb LIKE ?
        )
        """
        args.extend([f"%{q}%"] * 3)

    sql += " ORDER BY notice_date DESC LIMIT ?"
    args.append(max(1, min(int(limit), 5000)))

    return [
        dict(row)
        for row in con.execute(sql, args)
    ]
