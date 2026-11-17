"""Portable database storage for Insolvency Radar.

Uses PostgreSQL when DATABASE_URL is configured (Render/Neon), and SQLite
for local development and tests. Existing SQLite databases remain supported.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    Column, Float, ForeignKey, Index, Integer, MetaData, String, Table,
    UniqueConstraint, create_engine, event, text, func,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError

DB_PATH = os.environ.get("RADAR_DB", "radar.db")
IntegrityError = (sqlite3.IntegrityError, SQLAlchemyIntegrityError)

metadata = MetaData()

leads = Table(
    "leads", metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("status", String),
    Column("entity_name", String),
    Column("abn", String),
    Column("acn", String),
    Column("state", String),
    Column("suburb", String),
    Column("postcode", String),
    Column("asset_summary", String),
    Column("notice_date", String),
    Column("deadline", String),
    Column("contact_name", String),
    Column("contact_firm", String),
    Column("contact_phone", String),
    Column("contact_email", String),
    Column("source", String, nullable=False),
    Column("source_url", String),
    Column("raw_text", String),
    Column("confidence", Float, default=0.5),
    Column("first_seen", String, server_default=text("CURRENT_TIMESTAMP")),
)
Index("idx_leads_state", leads.c.state)
Index("idx_leads_kind", leads.c.kind)
Index("idx_leads_date", leads.c.notice_date)
Index("idx_leads_acn", leads.c.acn)

# Individual ASIC appointment events are kept separately from the leads summary.
asic_appointments = Table(
    "asic_appointments", metadata,
    Column("event_id", String, primary_key=True),
    Column("acn", String, nullable=False),
    Column("company_name", String),
    Column("appointee", String),
    Column("appointment_type", String),
    Column("category", String),
    Column("effective_date", String),
    Column("industry", String),
    Column("state", String),
    Column("area", String),
    Column("postcode", String),
    Column("source", String, nullable=False),
    Column("source_url", String),
    Column("created_at", String, server_default=text("CURRENT_TIMESTAMP")),
)
Index("idx_asic_appointments_acn", asic_appointments.c.acn)
Index("idx_asic_appointments_date", asic_appointments.c.effective_date)
Index("idx_asic_appointments_state", asic_appointments.c.state)

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("plan", String, nullable=False, server_default="free"),
    Column("is_active", Integer, nullable=False, server_default="1"),
    Column("email_verified_at", String),
    Column("created_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)
Index("idx_users_email_lower", func.lower(users.c.email), unique=True)

watchlists = Table(
    "watchlists", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("acn", String, nullable=False),
    Column("company_name", String),
    Column("created_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("user_id", "acn", name="uq_watchlists_user_acn"),
)
Index("idx_watchlists_user", watchlists.c.user_id)
Index("idx_watchlists_acn", watchlists.c.acn)

subscriptions = Table(
    "subscriptions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("stripe_customer_id", String),
    Column("stripe_subscription_id", String, unique=True),
    Column("stripe_price_id", String),
    Column("plan", String, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("current_period_start", String),
    Column("current_period_end", String),
    Column("cancel_at_period_end", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)
Index("idx_subscriptions_user", subscriptions.c.user_id)
Index("idx_subscriptions_customer", subscriptions.c.stripe_customer_id)

password_reset_tokens = Table(
    "password_reset_tokens", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", String, nullable=False, unique=True),
    Column("expires_at", String, nullable=False),
    Column("used_at", String),
    Column("created_at", String, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)
Index("idx_password_reset_user", password_reset_tokens.c.user_id)


class CompatRow(Mapping):
    """SQLAlchemy row supporting both row['column'] and row[0]."""
    def __init__(self, row):
        self._mapping = row._mapping
        self._values = tuple(row)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self):
        return len(self._mapping)


class CompatResult:
    def __init__(self, result):
        self._result = result
        self.rowcount = result.rowcount

    @property
    def lastrowid(self):
        return getattr(self._result, "lastrowid", None)

    def fetchone(self):
        row = self._result.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self):
        return [CompatRow(row) for row in self._result.fetchall()]

    def __iter__(self):
        for row in self._result:
            yield CompatRow(row)


class CompatConnection:
    def __init__(self, connection):
        self._connection = connection
        self.dialect_name = connection.dialect.name

    def execute(self, sql, params=None):
        params = () if params is None else params
        if isinstance(params, Mapping):
            statement = sql
            bound = dict(params)
        else:
            values = list(params) if isinstance(params, (tuple, list)) else [params]
            index = 0

            def replace(_match):
                nonlocal index
                name = f"p{index}"
                index += 1
                return f":{name}"

            statement = re.sub(r"\?", replace, sql)
            if len(values) != index:
                raise ValueError("SQL parameter count does not match placeholders")
            bound = {f"p{i}": value for i, value in enumerate(values)}
        return CompatResult(self._connection.execute(text(statement), bound))


    def executemany(self, sql, rows):
        rows = list(rows)
        if not rows:
            return None

        index = 0

        def replace(_match):
            nonlocal index
            name = f"p{index}"
            index += 1
            return f":{name}"

        statement = re.sub(r"\?", replace, sql)
        bound_rows = []

        for row in rows:
            values = list(row)
            if len(values) != index:
                raise ValueError(
                    "SQL parameter count does not match placeholders"
                )
            bound_rows.append({
                f"p{i}": value for i, value in enumerate(values)
            })

        return CompatResult(
            self._connection.execute(text(statement), bound_rows)
        )

def _database_url(path=None):
    if path is None:
        configured = os.environ.get("DATABASE_URL")
        if configured:
            if configured.startswith("postgres://"):
                configured = "postgresql+psycopg://" + configured[len("postgres://"):]
            elif configured.startswith("postgresql://"):
                configured = "postgresql+psycopg://" + configured[len("postgresql://"):]
            return configured
        path = DB_PATH

    target = os.path.abspath(os.fspath(path))
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + target.replace("\\", "/")


_engines: dict[str, Engine] = {}
_engine_lock = threading.Lock()


def _get_engine(path=None):
    url = _database_url(path)
    with _engine_lock:
        engine = _engines.get(url)
        if engine is None:
            if url.startswith("sqlite:"):
                engine = create_engine(
                    url,
                    connect_args={"timeout": 30, "check_same_thread": False},
                )

                @event.listens_for(engine, "connect")
                def _sqlite_pragmas(dbapi_connection, _record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()
            else:
                engine = create_engine(url, connect_args={"connect_timeout": 10}, pool_pre_ping=True, pool_recycle=300, pool_timeout=15, use_native_hstore=False)
            metadata.create_all(engine)
            _engines[url] = engine
        return engine


@contextmanager
def connect(path: str | None = None):
    """Open a transaction; use DATABASE_URL in production, SQLite locally."""
    engine = _get_engine(path)
    with engine.begin() as connection:
        yield CompatConnection(connection)


def begin_write(con: CompatConnection, user_id: int | None = None):
    """Serialize watchlist limit checks for one customer across workers."""
    if con.dialect_name == "sqlite":
        con._connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif user_id is not None:
        con.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user_id,)).fetchone()


def lead_id(source: str, source_ref: str) -> str:
    value = f"{source}|{source_ref}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


COLUMNS = [
    "id", "kind", "status", "entity_name", "abn", "acn", "state", "suburb",
    "postcode", "asset_summary", "notice_date", "deadline", "contact_name",
    "contact_firm", "contact_phone", "contact_email", "source", "source_url",
    "raw_text", "confidence",
]


def upsert(con, lead: dict) -> None:
    if not isinstance(lead, dict):
        raise TypeError("Lead must be a dictionary")
    row = {column: lead.get(column) for column in COLUMNS}
    for required in ("id", "kind", "source"):
        if not row[required] or not str(row[required]).strip():
            raise ValueError(f"Lead is missing required field: {required}")
    if row["confidence"] is None:
        row["confidence"] = 0.5

    placeholders = ",".join("?" for _ in COLUMNS)
    updates = []
    for column in COLUMNS:
        if column == "id":
            continue
        elif column == "confidence":
            if lead.get("confidence") is None:
                updates.append("confidence=leads.confidence")
            else:
                updates.append("confidence=excluded.confidence")
        elif column in ("kind", "source"):
            updates.append(f"{column}=excluded.{column}")
        else:
            updates.append(
                f"{column}=CASE WHEN excluded.{column} IS NOT NULL "
                f"AND excluded.{column} != '' THEN excluded.{column} "
                f"ELSE leads.{column} END"
            )
    con.execute(
        f"INSERT INTO leads ({','.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {','.join(updates)}",
        [row[column] for column in COLUMNS],
    )



def upsert_many(con, batch: list[dict]) -> int:
    if not batch:
        return 0

    rows = []

    for lead in batch:
        if not isinstance(lead, dict):
            raise TypeError("Lead must be a dictionary")

        row = {column: lead.get(column) for column in COLUMNS}

        for required in ("id", "kind", "source"):
            if not row[required] or not str(row[required]).strip():
                raise ValueError(
                    f"Lead is missing required field: {required}"
                )

        rows.append(row)
    if con.dialect_name == "postgresql":
        rows_with_confidence = []
        rows_without_confidence = []

        for row in rows:
            statement_row = dict(row)

            if statement_row["confidence"] is None:
                statement_row["confidence"] = 0.5
                rows_without_confidence.append(statement_row)
            else:
                rows_with_confidence.append(statement_row)

        def execute_pg_batch(statement_rows, preserve_confidence):
            if not statement_rows:
                return

            stmt = pg_insert(leads).values(statement_rows)
            excluded = stmt.excluded
            updates = {}

            for column in COLUMNS:
                if column == "id":
                    continue
                elif column == "confidence":
                    updates[column] = (
                        leads.c.confidence
                        if preserve_confidence
                        else excluded.confidence
                    )
                elif column in ("kind", "source"):
                    updates[column] = getattr(excluded, column)
                else:
                    incoming = getattr(excluded, column)
                    updates[column] = func.coalesce(
                        func.nullif(incoming, ""),
                        getattr(leads.c, column),
                    )

            stmt = stmt.on_conflict_do_update(
                index_elements=[leads.c.id],
                set_=updates,
            )

            con._connection.execute(stmt)

        execute_pg_batch(rows_without_confidence, True)
        execute_pg_batch(rows_with_confidence, False)

        return len(rows)

    placeholders = [
        "COALESCE(?, 0.5)" if column == "confidence" else "?"
        for column in COLUMNS
    ]

    updates = []

    for column in COLUMNS:
        if column == "id":
            continue
        elif column == "confidence":
            updates.append(
                "confidence=CASE WHEN ? IS NULL "
                "THEN leads.confidence ELSE excluded.confidence END"
            )
        elif column in ("kind", "source"):
            updates.append(f"{column}=excluded.{column}")
        else:
            updates.append(
                f"{column}=CASE WHEN excluded.{column} IS NOT NULL "
                f"AND excluded.{column} != '' THEN excluded.{column} "
                f"ELSE leads.{column} END"
            )

    sql = (
        f"INSERT INTO leads ({','.join(COLUMNS)}) "
        f"VALUES ({','.join(placeholders)}) "
        f"ON CONFLICT(id) DO UPDATE SET {','.join(updates)}"
    )

    sqlite_rows = [
        [row[column] for column in COLUMNS] + [row["confidence"]]
        for row in rows
    ]

    con.executemany(sql, sqlite_rows)
    return len(rows)



def appointment_event_id(appointment: dict) -> str:
    # Stable identity for one ASIC appointment event.
    fields = (
        str(appointment.get("acn") or "").strip(),
        str(appointment.get("date") or "").strip(),
        str(appointment.get("atype") or "").strip().casefold(),
        str(appointment.get("appointee") or "").strip().casefold(),
        str(appointment.get("name") or "").strip().casefold(),
        str(appointment.get("industry") or "").strip().casefold(),
        str(appointment.get("state") or "").strip().upper(),
        str(appointment.get("area") or "").strip().casefold(),
        str(appointment.get("postcode") or "").strip(),
    )
    value = "|".join(fields)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def upsert_asic_appointments(con, batch: list[dict]) -> int:
    # Idempotently store individual ASIC appointment events in batches.
    if not batch:
        return 0

    columns = [
        "event_id", "acn", "company_name", "appointee",
        "appointment_type", "category", "effective_date", "industry",
        "state", "area", "postcode", "source", "source_url",
    ]
    rows = []
    seen = set()

    for appointment in batch:
        if not isinstance(appointment, dict):
            raise TypeError("ASIC appointment must be a dictionary")
        row = {column: appointment.get(column) for column in columns}
        for required in ("event_id", "acn", "source"):
            if not row[required] or not str(row[required]).strip():
                raise ValueError(f"ASIC appointment is missing required field: {required}")
        if row["event_id"] in seen:
            continue
        seen.add(row["event_id"])
        rows.append(row)

    if not rows:
        return 0

    insert_fn = pg_insert if con.dialect_name == "postgresql" else sqlite_insert
    table = asic_appointments

    for start in range(0, len(rows), 500):
        chunk = rows[start:start + 500]
        stmt = insert_fn(table).values(chunk)
        excluded = stmt.excluded
        updates = {}
        for column in columns:
            if column == "event_id":
                continue
            incoming = getattr(excluded, column)
            updates[column] = func.coalesce(
                func.nullif(incoming, ""),
                getattr(table.c, column),
            )
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.event_id],
            set_=updates,
        )
        con._connection.execute(stmt)

    return len(rows)


def _lead_search_where(kind=None, state=None, status=None, q=None, since=None):
    sql = " WHERE 1=1"
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
        term = f"%{str(q).strip()}%"
        fields = ("entity_name", "asset_summary", "suburb", "postcode",
                  "acn", "abn", "contact_name", "contact_firm")
        sql += " AND (" + " OR ".join(
            "LOWER(COALESCE(" + field + ",'')) LIKE LOWER(?)" for field in fields
        ) + ")"
        args.extend([term] * len(fields))
    if since:
        sql += ' AND notice_date >= ?'
        args.append(since)
    return sql, args


def count_search(con, kind=None, state=None, status=None, q=None, since=None):
    where, args = _lead_search_where(kind, state, status, q, since)
    row = con.execute("SELECT COUNT(*) AS total FROM leads" + where, args).fetchone()
    return int(row["total"] if hasattr(row, "keys") else row[0])


def search(con, kind=None, state=None, status=None, q=None, limit=200, offset=0, since=None):
    where, args = _lead_search_where(kind, state, status, q, since)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 20000))
    offset = max(0, offset)
    sql = ("SELECT * FROM leads" + where +
           " ORDER BY notice_date DESC, first_seen DESC, LOWER(entity_name) ASC LIMIT ? OFFSET ?")
    args.extend([limit, offset])
    return [dict(row) for row in con.execute(sql, args)]


def upsert_many(con, leads: list[dict]) -> int:
    """Batch upsert leads while preserving existing upsert semantics."""
    if not leads:
        return 0

    groups = {True: [], False: []}

    for lead in leads:
        if not isinstance(lead, dict):
            raise TypeError("Lead must be a dictionary")

        row = {column: lead.get(column) for column in COLUMNS}
        for required in ("id", "kind", "source"):
            if not row[required] or not str(row[required]).strip():
                raise ValueError(
                    f"Lead is missing required field: {required}"
                )

        preserve_confidence = lead.get("confidence") is None
        if row["confidence"] is None:
            row["confidence"] = 0.5

        groups[preserve_confidence].append(
            tuple(row[column] for column in COLUMNS)
        )

    placeholders = ",".join("?" for _ in COLUMNS)
    updates = []

    for column in COLUMNS:
        if column == "id":
            continue
        if column == "confidence":
            updates.append(
                "confidence=leads.confidence"
                if True in groups and groups[True]
                else "confidence=excluded.confidence"
            )
        elif column in ("kind", "source"):
            updates.append(f"{column}=excluded.{column}")
        else:
            updates.append(
                f"{column}=CASE WHEN excluded.{column} IS NOT NULL "
                f"AND excluded.{column} != '' THEN excluded.{column} "
                f"ELSE leads.{column} END"
            )

    sql_template = (
        f"INSERT INTO leads ({','.join(COLUMNS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET "
    )

    total = 0
    for preserve, rows in groups.items():
        if not rows:
            continue

        group_updates = [
            "confidence=leads.confidence"
            if column == "confidence" and preserve
            else "confidence=excluded.confidence"
            if column == "confidence"
            else update
            for column, update in zip(
                [c for c in COLUMNS if c != "id"], updates
            )
        ]

        con.executemany(
            sql_template + ",".join(group_updates),
            rows
        )
        total += len(rows)

    return total
