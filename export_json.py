"""Write the leads database to a JSON file for the static website."""
import json
import os
import sys
import time

import db


def export(path="leads.json", sample=False):
    with db.connect() as con:
        rows = db.search(con, limit=5000)
    for r in rows:
        r.pop("raw_text", None)
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "sample": sample, "count": len(rows)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "leads": rows}, f, ensure_ascii=False)
    return len(rows)


if __name__ == "__main__":
    print(f"exported {export(sample='--sample' in sys.argv)} leads")
