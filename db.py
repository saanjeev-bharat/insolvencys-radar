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
                engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, use_native_hstore=False)
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


def search(con, kind=None, state=None, status=None, q=None, limit=200):
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
        sql += " AND (LOWER(entity_name) LIKE LOWER(?) OR LOWER(asset_summary) LIKE LOWER(?) OR LOWER(suburb) LIKE LOWER(?))"
        term = f"%{str(q).strip()}%"
        args.extend([term] * 3)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 5000))
    sql += " ORDER BY notice_date DESC, first_seen DESC, LOWER(entity_name) ASC LIMIT ?"
    args.append(limit)
    return [dict(row) for row in con.execute(sql, args)]
