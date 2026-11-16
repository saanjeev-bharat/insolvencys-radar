
import argparse
import json
import os
from collections import Counter
from pathlib import Path

import db


SOURCE = "ASIC Company Dataset - leads.json"
SOURCE_URL = "https://asic.gov.au/"

CATEGORY_STATUS = {
    "L": "liquidation",
    "A": "administration",
    "R": "controller_receivership",
    "E": "external_administration",
    "S": "strike_off",
    "O": "other_appointment",
}


def classify_status(category, appointment):
    if category == "R":
        description = (appointment or "").strip().lower()

        if description.startswith("receiver"):
            return "receivership"

        if description.startswith("managing controller"):
            return "managing_controller"

        return "controller_appointment"

    return CATEGORY_STATUS[category]

COMPANY_STATUS = {
    "EXAD": "External administration",
    "SOFF": "Strike-off in progress",
    "REGD": "Registered",
    "DRGD": "Deregistered",
    "NOAC": "Not active",
}


def clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def make_lead(record):
    acn = clean(record.get("a"))
    category = clean(record.get("k"))
    company_status = clean(record.get("s"))

    if not acn:
        raise ValueError("Record has no ACN")

    if category not in CATEGORY_STATUS:
        raise ValueError(
            f"Unknown ASIC category {category!r} for ACN {acn}"
        )

    name = clean(record.get("n"))
    if not name:
        raise ValueError(f"Record {acn} has no company name")

    appointment = clean(record.get("p"))
    industry = clean(record.get("in"))

    summary = appointment or industry
    if not summary:
        summary = COMPANY_STATUS.get(
            company_status, "ASIC company record"
        )

    raw_fields = [
        ("ASIC company status", COMPANY_STATUS.get(
            company_status, company_status
        )),
        ("ASIC category", category),
        ("Appointment", appointment),
        ("Appointment date", clean(record.get("d"))),
        ("Appointee", clean(record.get("ap"))),
        ("Registration date", clean(record.get("r"))),
        ("Industry", industry),
        ("Appointment count", clean(record.get("na"))),
        ("First seen", clean(record.get("f"))),
    ]

    raw_text = "\n".join(
        f"{label}: {value}"
        for label, value in raw_fields
        if value is not None
    )

    return {
        "id": db.lead_id(SOURCE, acn),
        "kind": "asic_company",
        "status": classify_status(category, appointment),
        "entity_name": name,
        "abn": clean(record.get("b")),
        "acn": acn,
        "state": clean(record.get("st")),
        "suburb": clean(record.get("ar")),
        "postcode": clean(record.get("pc")),
        "asset_summary": summary,
        "notice_date": clean(record.get("d")),
        "deadline": None,
        "contact_name": None,
        "contact_firm": None,
        "contact_phone": None,
        "contact_email": None,
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "raw_text": raw_text,
        "confidence": 1.0,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Import ASIC company dataset into the leads table."
    )
    parser.add_argument(
        "--file",
        default="leads.json",
        help="Input JSON file (default: leads.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write records to the configured database.",
    )
    parser.add_argument(
        "--allow-postgres",
        action="store_true",
        help="Explicitly permit writes when DATABASE_URL is configured.",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"Input file not found: {path}")

    database_url = os.environ.get("DATABASE_URL")
    if database_url and args.apply and not args.allow_postgres:
        raise SystemExit(
            "Safety stop: DATABASE_URL is configured. "
            "PostgreSQL writes require --allow-postgres."
        )

    if args.allow_postgres and not database_url:
        raise SystemExit(
            "--allow-postgres was supplied, but DATABASE_URL is not set."
        )

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    records = data.get("leads") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise SystemExit("Invalid JSON: expected a 'leads' list.")

    prepared = []
    categories = Counter()
    identifiers = set()

    for number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise SystemExit(f"Invalid record at position {number}")

        lead = make_lead(record)

        if lead["id"] in identifiers:
            raise SystemExit(
                f"Duplicate importer ID for ACN {lead['acn']}"
            )

        identifiers.add(lead["id"])
        categories[record.get("k")] += 1
        prepared.append(lead)

    print("ASIC import validation successful.")
    print(f"Input file: {path}")
    print(f"Records validated: {len(prepared)}")
    print(f"Unique importer IDs: {len(identifiers)}")
    print(f"Source identifier: {SOURCE}")
    print("\nCategory counts:")

    for category, count in sorted(categories.items()):
        print(f"  {category}: {count}")

    if not args.apply:
        print("\nDRY RUN ONLY: No database records were written.")
        print("Use --apply only after the local test is approved.")
        return

    imported = 0
    batch_size = 500

    with db.connect() as con:
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start:start + batch_size]

            for lead in batch:
                db.upsert(con, lead)

            imported += len(batch)
            print(
                f"Imported {imported:,} / {len(prepared):,} records",
                flush=True,
            )

    print(f"\nImport committed successfully: {imported} records.")

if __name__ == "__main__":
    main()
