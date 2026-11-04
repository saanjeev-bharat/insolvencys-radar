"""Weekly job: gather Australia-wide insolvency data from ASIC's open sources and write leads.json.

Sources (all free, official):
  1. ASIC insolvency statistics, Series 1 and 2: recent external administration appointments,
     with the appointee, appointment type and date, industry, state and postcode.
  2. ASIC Company Dataset (data.gov.au): each company's current status, ABN, plus companies
     in external administration (EXAD) or facing strike-off (SOFF).
  3. ASIC register of liquidators (data.gov.au): firm and town hint for the appointee.
"""
import datetime
import json
import os
import sys
import tempfile
import time
from collections import Counter

import asic_companies as C
import asic_liquidators as L
import asic_stats as S

KIND = "asic_multi_source"
OLD_KINDS = (KIND, "asic_company_dataset")


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}, None
    meta = d.get("meta") or {}
    if meta.get("sample") or meta.get("kind") not in OLD_KINDS:
        return {}, None
    return {r["a"]: r.get("f") for r in d.get("leads", []) if r.get("a")}, meta.get("baseline")


def build_leads(latest, companies, people, previous, today):
    leads = {}
    for acn, a in latest.items():
        c = companies.get(acn, {})
        rec = {
            "n": a["name"] or c.get("n", ""), "a": acn, "b": c.get("b", ""), "s": c.get("s", ""),
            "t": c.get("t", ""), "c": c.get("c", ""), "r": c.get("r", ""),
            "k": a["cat"], "p": a["atype"], "d": a["date"], "ap": a["appointee"],
            "st": a["state"], "ar": a["area"], "pc": a["postcode"], "in": a["industry"],
            "na": a.get("count", 1),
        }
        hit = L.match(a["appointee"], people) if a["appointee"] and people else None
        if hit:
            rec["fm"], rec["pt"] = hit["firm"], ", ".join(x for x in (hit["town"], hit["state"]) if x)
        leads[acn] = rec
    for acn, c in companies.items():
        if acn in leads or c.get("s") not in C.WANTED:
            continue
        leads[acn] = {"n": c["n"], "a": acn, "b": c.get("b", ""), "s": c["s"], "t": c.get("t", ""),
                      "c": c.get("c", ""), "r": c.get("r", ""), "k": "E" if c["s"] == "EXAD" else "S"}
    out = []
    for acn, rec in leads.items():
        rec = {k: v for k, v in rec.items() if v not in ("", None)}
        rec["f"] = previous.get(acn) or today
        out.append(rec)
    out.sort(key=lambda r: r["n"])
    out.sort(key=lambda r: r["f"], reverse=True)
    out.sort(key=lambda r: r.get("d", ""), reverse=True)
    return out


def run(out="leads.json", months=None, paths=None):
    paths = paths or {}
    months = months or float(os.environ.get("RADAR_MONTHS", "12"))
    today = datetime.date.today().isoformat()
    previous, baseline = load_previous(out)
    sources, info = {}, {}
    td = tempfile.mkdtemp()

    latest = {}
    try:
        latest, info["appointments"] = S.load(months=months, path=paths.get("stats"), workdir=td)
        sources["appointments"] = "ok"
    except Exception as e:
        sources["appointments"] = "failed: %s" % e
        print("WARNING appointments:", e, file=sys.stderr)

    companies, counts = {}, Counter()
    try:
        path = paths.get("companies")
        if path is None:
            url = os.environ.get("RADAR_COMPANY_URL") or C.pick_resource(C.fetch_listing())
            print("company dataset file:", url)
            path = os.path.join(td, "company.dat")
            C.download(url, path)
            info["company_file"] = url
        companies, counts = C.collect(C.iter_rows(path), extra=set(latest))
        sources["company_status"] = "ok"
    except Exception as e:
        sources["company_status"] = "failed: %s" % e
        print("WARNING company dataset:", e, file=sys.stderr)

    people = []
    try:
        people, url = L.load(path=paths.get("liquidators"), workdir=td)
        sources["liquidator_register"] = "ok"
    except Exception as e:
        sources["liquidator_register"] = "failed: %s" % e
        print("WARNING liquidator register:", e, file=sys.stderr)

    leads = build_leads(latest, companies, people, previous, today)
    if not leads:
        raise RuntimeError("no data collected from any source: %s" % sources)
    if sources["appointments"] != "ok" and not previous:
        print("NOTE: appointments missing, showing company status data only", file=sys.stderr)

    meta = {
        "kind": KIND, "sample": False,
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "baseline": baseline or today, "count": len(leads), "months": months,
        "sources": sources,
        "by_category": dict(Counter(r.get("k", "?") for r in leads)),
        "by_state": dict(Counter(r["st"] for r in leads if r.get("st"))),
        "with_practitioner": sum(1 for r in leads if r.get("ap")),
        "with_firm": sum(1 for r in leads if r.get("fm")),
        "files": {k: v for k, v in info.items() if isinstance(v, str)},
        "columns_used": (info.get("appointments") or {}).get("columns"),
        "company_status_counts": dict(counts.most_common(12)),
    }
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "leads": leads}, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    print("sources:", sources)
    print("leads:", len(leads), "| with practitioner:", meta["with_practitioner"], "| with firm:", meta["with_firm"])
    return len(leads)


if __name__ == "__main__":
    try:
        print(f"wrote {run()} companies")
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
