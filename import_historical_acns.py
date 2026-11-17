#!/usr/bin/env python3
"""Safely classify historical ASIC ACNs in Neon; dry-run unless --apply is supplied."""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

from sqlalchemy import bindparam, create_engine, inspect, text

CSV_PATH = Path(__file__).with_name("neon_only_acns.csv")
EXPECTED_ROWS = 5394
SOURCE = "Historical ASIC ACNs - reconciled 2026-11-17"
BATCH_SIZE = 500
ACN_RE = re.compile(r"^\d{9}$")


def load_acns():
    if not CSV_PATH.is_file():
        raise RuntimeError(f"Required file not found: {CSV_PATH.name}")
    values = []
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "acn" not in reader.fieldnames:
            raise RuntimeError("CSV must contain an 'acn' header.")
        for row_num, row in enumerate(reader, start=2):
            acn = (row.get("acn") or "").strip()
            if not ACN_RE.fullmatch(acn):
                raise RuntimeError(f"Invalid or blank 9-digit ACN at CSV row {row_num}: {acn!r}")
            values.append(acn)
    if len(values) != EXPECTED_ROWS:
        raise RuntimeError(f"Expected exactly {EXPECTED_ROWS} rows; found {len(values)}.")
    if len(set(values)) != len(values):
        raise RuntimeError("Duplicate ACNs found in CSV; refusing to continue.")
    return values


def chunks(items, size=BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Insert missing classifications. Without this flag, performs dry-run only.")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Refusing to use local SQLite.")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must point to PostgreSQL via psycopg. Refusing other databases.")

    acns = load_acns()
    print(f"CSV validation: PASS ({len(acns)} unique, valid 9-digit ACNs)")
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 15})

    with engine.connect() as conn:
        if conn.dialect.name != "postgresql":
            raise RuntimeError("Connected database is not PostgreSQL.")
        inspector = inspect(conn)
        if not inspector.has_table("leads"):
            raise RuntimeError("Neon table 'leads' does not exist.")
        if not inspector.has_table("asic_historical_acns"):
            raise RuntimeError(
                "Neon table 'asic_historical_acns' does not exist. "
                "No changes made; deploy/start the API so its existing metadata creates the table, then rerun."
            )

        found = set()
        already = set()
        for batch in chunks(acns):
            stmt = text(
                "SELECT acn FROM leads "
                "WHERE kind = 'asic_company' AND TRIM(COALESCE(acn, '')) IN :values"
            ).bindparams(bindparam("values", expanding=True))
            found.update(row[0].strip() for row in conn.execute(stmt, {"values": batch}) if row[0])

            hist_stmt = text(
                "SELECT acn FROM asic_historical_acns "
                "WHERE TRIM(COALESCE(acn, '')) IN :values"
            ).bindparams(bindparam("values", expanding=True))
            already.update(row[0].strip() for row in conn.execute(hist_stmt, {"values": batch}) if row[0])

        missing = sorted(set(acns) - found)
        eligible = sorted(set(acns) & found - already)
        print(f"Historical ACNs found in Neon leads (kind=asic_company): {len(found)}")
        print(f"Already classified historical: {len(already)}")
        print(f"Missing from Neon leads: {len(missing)}")
        print(f"New classifications eligible: {len(eligible)}")

        if missing:
            print("STOP: some CSV ACNs are absent from the ASIC company records. No changes made.")
            return 2

        if not args.apply:
            print("DRY-RUN ONLY: no database records were changed.")
            print("Review these counts, then rerun the GitHub workflow with apply=true to insert.")
            return 0

        if eligible:
            insert_stmt = text(
                "INSERT INTO asic_historical_acns (acn, source) "
                "VALUES (:acn, :source) ON CONFLICT (acn) DO NOTHING"
            )
            inserted = 0
            with engine.begin() as write_conn:
                for batch in chunks(eligible):
                    result = write_conn.execute(
                        insert_stmt,
                        [{"acn": acn, "source": SOURCE} for acn in batch]
                    )
                    inserted += max(result.rowcount or 0, 0)
        else:
            inserted = 0

        final_count = conn_count = None
        with engine.connect() as verify_conn:
            final_count = verify_conn.execute(
                text("SELECT COUNT(*) FROM asic_historical_acns")
            ).scalar_one()
        print(f"New classifications inserted: {inserted}")
        print(f"Total rows in asic_historical_acns after import: {final_count}")
        print("Only asic_historical_acns was written. The leads, users, watchlists and subscriptions tables were not modified.")
    engine.dispose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
