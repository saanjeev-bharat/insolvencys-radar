import sys

import db, extract, sources as S


def run(srcs=None, db_path=None) -> int:
    srcs = srcs or S.default_sources()
    n = 0
    with db.connect(db_path) as con:
        for src in srcs:
            try:
                for item in src.fetch():
                    lead = extract.extract_regex(item["text"], item["kind"])
                    lead = extract.refine_with_claude(item["text"], lead)
                    if item["kind"] != "insolvency" and not lead.get("status"):
                        lead["status"] = item["kind"]
                    if not (lead.get("entity_name") or lead.get("asset_summary") or lead.get("status")):
                        continue  # not a distress notice
                    lead.update(id=db.lead_id(item["source"], item["source_ref"]),
                                source=item["source"], source_url=item.get("source_url"))
                    db.upsert(con, lead)
                    n += 1
            except Exception as e:  # one bad source must not stop the rest
                print(f"[warn] {src.name}: {e}", file=sys.stderr)
    return n


if __name__ == "__main__":
    print(f"ingested {run()} leads")
