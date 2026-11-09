
"""SQLite storage for Insolvency Radar.

One table of distress leads, deduplicated by a stable key.
Existing non-empty information is preserved during updates.
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

    con = sqlite3.connect(target, timeout=30)

    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
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
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


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
    if not isinstance(lead, dict):
        raise TypeError("Lead must be a dictionary")

    row = {column: lead.get(column) for column in COLUMNS}

    for required in ("id", "kind", "source"):
        if not row[required] or not str(row[required]).strip():
            raise ValueError(f"Lead is missing required field: {required}")

    # Use the default confidence for new records.
    if row["confidence"] is None:
        row["confidence"] = 0.5

    placeholders = ",".join("?" for _ in COLUMNS)

    updates = []

    for column in COLUMNS:
        if column == "id":
            continue

        if column == "confidence" and lead.get("confidence") is None:
            updates.append("confidence=leads.confidence")
        elif column in ("kind", "source"):
            updates.append(f"{column}=excluded.{column}")
        else:
            updates.append(
                f"""
                {column}=CASE
                    WHEN excluded.{column} IS NOT NULL
                    AND excluded.{column} != ''
                    THEN excluded.{column}
                    ELSE leads.{column}
                END
                """
            )

    con.execute(
        f"""
        INSERT INTO leads ({','.join(COLUMNS)})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {','.join(updates)}
        """,
        [row[column] for column in COLUMNS],
    )


def search(
    con,
    kind=None,
    state=None,
    status=None,
    q=None,
    limit=200,
):
    sql = "SELECT * FROM leads WHERE 1=1"
    args = []

    if kind:
        sql += " AND kind=?"
        args.append(kind)

    if state:
        sql += " AND state=?"
        args.append(state.strip().upper())

    if status:
        sql += " AND status=?"
        args.append(status)

    if q and str(q).strip():
        sql += """
        AND (
            entity_name LIKE ?
            OR asset_summary LIKE ?
            OR suburb LIKE ?
        )
        """
        term = f"%{str(q).strip()}%"
        args.extend([term] * 3)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200

    limit = max(1, min(limit, 5000))

    sql += """
        ORDER BY
            notice_date DESC,
            first_seen DESC,
            entity_name COLLATE NOCASE ASC
        LIMIT ?
    """
    args.append(limit)

    return [
        dict(row)
        for row in con.execute(sql, args)
    ]
