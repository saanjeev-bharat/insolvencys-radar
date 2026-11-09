
import sys

import db
import extract
import sources as S
import asic_stats


def import_asic_stats(con):
    records, info = asic_stats.load(months=12)

    count = 0

    status_map = {
        "L": "liquidation",
        "A": "administration",
        "R": "receivership",
    }

    for acn, item in records.items():
        if item.get("cat") not in status_map:
            continue

        source = "ASIC Series 1 and 2"

        raw_text = (
            f"ASIC appointment: {item.get('atype', '')}; "
            f"Appointee: {item.get('appointee', '')}; "
            f"Industry: {item.get('industry', '')}; "
            f"Appointments in period: {item.get('count', 0)}"
        )

        lead = {
            "id": db.lead_id(source, acn),
            "kind": "insolvency",
            "status": status_map[item.get("cat")],
            "entity_name": item.get("name"),
            "acn": acn,
            "state": item.get("state"),
            "suburb": item.get("area"),
            "postcode": item.get("postcode"),
            "asset_summary": item.get("industry"),
            "notice_date": item.get("date"),
            "source": source,
            "source_url": "https://asic.gov.au/",
            "raw_text": raw_text,
            "confidence": 1.0,
        }

        db.upsert(con, lead)
        count += 1

    print(f"ASIC statistics: imported {count} companies")
    return count


def run(srcs=None, db_path=None) -> int:
    use_default_sources = srcs is None

    if use_default_sources:
        srcs = S.default_sources()

    n = 0

    with db.connect(db_path) as con:

        if use_default_sources:
            try:
                n += import_asic_stats(con)
            except Exception as e:
                print(
                    f"[warn] ASIC statistics: {e}",
                    file=sys.stderr,
                )

        for src in srcs:
            try:
                for item in src.fetch():

                    lead = extract.extract_regex(
                        item["text"],
                        item["kind"],
                    )

                    lead = extract.refine_with_claude(
                        item["text"],
                        lead,
                    )

                    if (
                        item["kind"] != "insolvency"
                        and not lead.get("status")
                    ):
                        lead["status"] = item["kind"]

                    if not (
                        lead.get("entity_name")
                        or lead.get("asset_summary")
                        or lead.get("status")
                    ):
                        continue

                    lead.update(
                        id=db.lead_id(
                            item["source"],
                            item["source_ref"],
                        ),
                        source=item["source"],
                        source_url=item.get("source_url"),
                    )

                    db.upsert(con, lead)
                    n += 1

            except Exception as e:
                print(
                    f"[warn] {src.name}: {e}",
                    file=sys.stderr,
                )

    return n


if __name__ == "__main__":
    print(f"ingested {run()} leads")
