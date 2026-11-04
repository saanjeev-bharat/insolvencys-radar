"""Companies in external administration or strike-off, from ASIC's open Company Dataset.

Source: "ASIC - Company Dataset" on data.gov.au, licensed CC BY 3.0 AU.
Status codes (from ASIC's data dictionary):
    EXAD  External administration (in receivership/liquidation)
    SOFF  Strike-off action in progress
The file is tab-separated (company names can contain commas) and may be zipped.
"""
import csv
import datetime
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import Counter

PACKAGE_ID = "7b8656f9-606d-4337-af29-66b89b2eeefb"
API = f"https://data.gov.au/data/api/3/action/package_show?id={PACKAGE_ID}"
WANTED = ("EXAD", "SOFF")
UA = os.environ.get("RADAR_USER_AGENT", "InsolvencyRadar/0.2 (github-actions)")
KIND = "asic_company_dataset"


# ---------- finding and downloading the file ----------

def pick_resource(resources):
    """Choose the current Company Dataset file from the dataset's resource list."""
    cands = []
    for r in resources or []:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        fmt = (r.get("format") or "").strip().lower()
        low = url.lower().split("?")[0]
        if fmt in ("csv", "zip") or low.endswith((".csv", ".zip")):
            cands.append(r)
    if not cands:
        raise RuntimeError("no CSV or ZIP file found in the dataset listing")

    def key(r):
        text = ((r.get("name") or "") + " " + (r.get("description") or "")).lower()
        return (1 if "current" in text else 0, r.get("last_modified") or r.get("created") or "")

    return max(cands, key=key)["url"]


def fetch_listing(package_id=PACKAGE_ID):
    import httpx
    api = f"https://data.gov.au/data/api/3/action/package_show?id={package_id}"
    r = httpx.get(api, headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("data.gov.au did not return the dataset listing")
    return data["result"]["resources"]


def download(url, dest):
    import httpx
    last = None
    for attempt in range(3):
        try:
            with httpx.stream("GET", url, headers={"User-Agent": UA},
                              timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            return
        except Exception as e:  # retry a few times
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download failed: {last}")


# ---------- reading the file ----------

def iter_rows(path):
    """Yield one dict per data row. Handles .zip or plain files, tab or comma separated."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt", ".tsv"))] or zf.namelist()
        raw = zf.open(names[0])
    else:
        raw = open(path, "rb")
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
    try:
        first = text.readline()
        if not first:
            return
        tab = "\t" in first
        kw = {"delimiter": "\t", "quoting": csv.QUOTE_NONE} if tab else {"delimiter": ","}
        header = [h.strip().strip('"') for h in next(csv.reader([first], **kw))]
        for fields in csv.reader(text, **kw):
            if fields:
                yield dict(zip(header, fields))
    finally:
        text.close()


# ---------- turning rows into records ----------

def _clean(s):
    return (s or "").strip().strip('"').strip()


def _iso_date(s):
    s = _clean(s)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def collect(rows, extra=None):
    """Return (records_by_acn, status_counts).

    Keeps companies with a wanted status (EXAD, SOFF) plus any ACN in `extra`,
    whatever its status, so appointments can be matched to the company's current status.
    """
    extra = extra or set()
    found, counts = {}, Counter()
    for row in rows:
        code = _clean(row.get("Status")).upper()
        counts[code] += 1
        if code not in WANTED and not extra:
            continue
        if _clean(row.get("Current Name Indicator")).upper() == "N":
            continue  # a former name of a company already listed under its current name
        acn = re.sub(r"\D", "", row.get("ACN") or "")
        if not acn:
            continue
        acn = acn.zfill(9)
        if code not in WANTED and acn not in extra:
            continue
        if acn in found:
            continue
        abn = re.sub(r"\D", "", row.get("ABN") or "")
        found[acn] = {
            "n": _clean(row.get("Company Name")),
            "a": acn,
            "b": abn if len(abn) == 11 else "",
            "s": code,
            "t": _clean(row.get("Type")),
            "c": _clean(row.get("Class")),
            "r": _iso_date(row.get("Date of Registration")),
        }
    return found, counts


def load_previous(path):
    """First-seen dates and baseline date from the last real run, so 'New' badges survive."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}, None
    meta = d.get("meta") or {}
    if meta.get("sample") or meta.get("kind") != KIND:
        return {}, None
    return {r["a"]: r.get("f") for r in d.get("leads", []) if r.get("a")}, meta.get("baseline")


def build(found, counts, previous, baseline, today, source_url=""):
    leads = []
    for acn, rec in found.items():
        rec = dict(rec)
        rec["f"] = previous.get(acn) or today
        leads.append(rec)
    leads.sort(key=lambda r: r["n"])
    leads.sort(key=lambda r: r["f"], reverse=True)
    meta = {
        "kind": KIND,
        "sample": False,
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "baseline": baseline or today,
        "count": len(leads),
        "by_status": dict(Counter(r["s"] for r in leads)),
        "all_status_counts": dict(counts),
        "dataset_file": source_url,
    }
    return {"meta": meta, "leads": leads}


def run(out="leads.json", url=None, path=None):
    today = datetime.date.today().isoformat()
    previous, baseline = load_previous(out)
    with tempfile.TemporaryDirectory() as td:
        if path is None:
            url = url or os.environ.get("RADAR_COMPANY_URL") or pick_resource(fetch_listing())
            print("dataset file:", url)
            path = os.path.join(td, "company.dat")
            download(url, path)
            print("downloaded", os.path.getsize(path) // (1 << 20), "MiB")
        found, counts = collect(iter_rows(path))
    print("status counts in file:", dict(counts.most_common(12)))
    if not found:
        raise RuntimeError("no companies with status EXAD or SOFF were found; "
                           "the file format may have changed. Status counts: " + str(dict(counts)))
    data = build(found, counts, previous, baseline, today, url or "")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    print("by status:", data["meta"]["by_status"])
    return len(data["leads"])


if __name__ == "__main__":
    try:
        print(f"wrote {run()} companies")
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
