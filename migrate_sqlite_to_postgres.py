"""Copy existing Insolvency Radar SQLite records into DATABASE_URL.

Usage (after DATABASE_URL is set to the Neon connection string):
    python migrate_sqlite_to_postgres.py radar.db

Rows are inserted without overwriting existing PostgreSQL records.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import db
from sqlalchemy import text

TABLES = (
    "leads",
    "users",
    "watchlists",
    "subscriptions",
    "password_reset_tokens",
)
IDENTITY_TABLES = ("users", "watchlists", "subscriptions", "password_reset_tokens")


def migrate(source_path: str) -> dict[str, int]:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set. Set the Neon connection string first.")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    copied: dict[str, int] = {}

    try:
        with db.connect() as target:
            for table in TABLES:
                exists = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    copied[table] = 0
                    continue

                source_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                target_columns = [column.name for column in db.metadata.tables[table].columns]
                columns = [column for column in target_columns if column in source_columns]
                if not columns:
                    copied[table] = 0
                    continue

                quoted_columns = ", ".join(f'"{column}"' for column in columns)
                binds = ", ".join(f":v{i}" for i in range(len(columns)))
                statement = text(
                    f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({binds}) ON CONFLICT DO NOTHING'
                )
                cursor = source.execute(f'SELECT {quoted_columns} FROM "{table}"')
                count = 0
                while True:
                    batch = cursor.fetchmany(500)
                    if not batch:
                        break
                    for row in batch:
                        target._connection.execute(statement, {f"v{i}": row[column] for i, column in enumerate(columns)})
                        count += 1
                copied[table] = count

            if target.dialect_name == "postgresql":
                for table in IDENTITY_TABLES:
                    target._connection.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                            f"EXISTS(SELECT 1 FROM {table}))"
                        )
                    )

    finally:
        source.close()

    return copied


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "radar.db"
    results = migrate(path)
    for table, count in results.items():
        print(f"{table}: {count} source rows processed")
    print("SQLite-to-PostgreSQL migration finished. Existing PostgreSQL rows were not overwritten.")
