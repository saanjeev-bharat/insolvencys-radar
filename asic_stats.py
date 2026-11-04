"""ASIC insolvency statistics, Series 1 and 2: one record per external administration or
controller appointment (company, ACN, appointee, appointment type and date, industry, state,
principal place of business and postcode). Published weekly by ASIC, two weeks in arrears.

The workbook's file name changes every week, so the download link is read from ASIC's page.
If ASIC's site refuses the download, put a copy of the workbook in the repository as
asic_series.xlsx and it is used instead.
"""
import datetime
import os
import re
import tempfile
from urllib.parse import urljoin

import asic_companies as C

PAGE = ("https://asic.gov.au/regulatory-resources/find-a-document/statistics/"
        "insolvency-statistics/insolvency-statistics-series-1-companies-entering-external-administration/")
LOCAL = "asic_series.xlsx"


def find_xlsx_url(html, base=PAGE):
    links = re.findall(r'href=["\']([^"\']+\.xlsx[^"\']*)["\']', html, flags=re.I)
    good = [l for l in links if "series-1" in l.lower() and "series-2" in l.lower()]
    if not good:
        raise RuntimeError("could not find the Series 1 and 2 Excel link on ASIC's statistics page")
    return urljoin(base, good[0].replace("&amp;", "&"))


def fetch_workbook(dest):
    """Download this week's workbook. Returns the URL used."""
    import httpx
    r = httpx.get(PAGE, headers={"User-Agent": C.UA}, timeout=60, follow_redirects=True)
    r.raise_for_status()
    url = find_xlsx_url(r.text)
    C.download(url, dest)
    return url


# ---------- reading the raw-data sheet ----------

def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s if s is not None else "").lower()).strip()


def _find(h, patterns, avoid=()):
    for exact in (True, False):
        for p in patterns:
            for i, x in enumerate(h):
                if not x or any(a in x for a in avoid):
                    continue
                if (x == p) if exact else (p in x):
                    return i
    return None


def map_columns(header):
    h = [_norm(x) for x in header]
    m = {
        "acn": _find(h, ["acn"], avoid=["appointee"]),
        "name": _find(h, ["organisation name", "company name", "organization name", "entity name", "name"],
                      avoid=["appointee", "industry", "anzsic"]),
        "appointee": _find(h, ["appointee name", "appointee"], avoid=["acn"]),
        "atype": _find(h, ["appointment type", "type of appointment", "appointment"],
                       avoid=["date", "financial", "appointee"]),
        "adate": _find(h, ["appointment start date", "start date", "appointment date", "date of appointment", "date"],
                       avoid=["financial", "year", "end"]),
        "area": _find(h, ["area", "suburb", "locality", "city", "town"], avoid=["industry"]),
        "postcode": _find(h, ["postcode", "post code"]),
    }
    ind = None
    for i, x in enumerate(h):
        if "division" in x and any(k in x for k in ("desc", "name", "title")):
            ind = i
            break
    if ind is None:
        ind = _find(h, ["anzsic division", "division", "industry"], avoid=["code"])
    m["industry"] = ind
    st = None
    for i, x in enumerate(h):
        if "state" in x and any(k in x for k in ("business", "location", "principal", "place")):
            st = i
            break
    if st is None:
        st = _find(h, ["state"], avoid=["incorporation"])
    m["state"] = st
    m["state_inc"] = _find(h, ["state of incorporation", "incorporation"])
    return m


def _date(v):
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def category(atype):
    x = (atype or "").lower()
    if "liquidat" in x:
        return "L"
    if "admin" in x or "deed" in x:
        return "A"
    if "receiver" in x or "controller" in x:
        return "R"
    return "O"


def _state(v):
    s = str(v or "").strip().upper()
    return s if s in {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"} else s[:3]


def read_appointments(path, since=None):
    """Return (appointments, info). Looks for the raw-data sheet from the last tab backwards."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    tried = []
    for ws in reversed(wb.worksheets):
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        header, mapping, hidx = None, None, None
        rows = ws.iter_rows(values_only=True)
        buf = []
        for i, row in enumerate(rows):
            if i > 40:
                break
            buf.append(row)
            cand = map_columns(row)
            if cand["acn"] is not None and cand["name"] is not None and (cand["atype"] is not None or cand["adate"] is not None):
                header, mapping, hidx = row, cand, i
                break
        tried.append(ws.title)
        if mapping is None:
            continue
        out = []
        g = lambda r, k: (r[mapping[k]] if mapping.get(k) is not None and mapping[k] < len(r) else None)
        for row in rows:  # continues after the header row
            acn = re.sub(r"\D", "", str(g(row, "acn") or ""))
            if len(acn) < 8:
                continue
            d = _date(g(row, "adate"))
            if since and d and d < since:
                continue
            atype = str(g(row, "atype") or "").strip()
            out.append({
                "acn": acn.zfill(9),
                "name": str(g(row, "name") or "").strip(),
                "appointee": str(g(row, "appointee") or "").strip(),
                "atype": atype,
                "cat": category(atype),
                "date": d,
                "industry": str(g(row, "industry") or "").strip(),
                "state": _state(g(row, "state")) or _state(g(row, "state_inc")),
                "area": str(g(row, "area") or "").strip().title(),
                "postcode": re.sub(r"\D", "", str(g(row, "postcode") or ""))[:4],
            })
        info = {"sheet": ws.title, "header_row": hidx + 1,
                "columns": {k: (str(header[v]).strip() if v is not None else None) for k, v in mapping.items()}}
        wb.close()
        return out, info
    wb.close()
    raise RuntimeError("could not find the raw appointment data in the workbook (sheets checked: %s)" % ", ".join(tried))


def latest_by_acn(appts):
    """Newest appointment per company, plus how many appointments it has in the period."""
    best, count = {}, {}
    for a in appts:
        count[a["acn"]] = count.get(a["acn"], 0) + 1
        cur = best.get(a["acn"])
        if cur is None or (a["date"] or "") > (cur["date"] or ""):
            best[a["acn"]] = a
    for acn, a in best.items():
        a["count"] = count[acn]
    return best


def load(months=12, path=None, workdir=None):
    """Download (or use the local copy) and read recent appointments. Returns (latest_by_acn, info)."""
    since = (datetime.date.today() - datetime.timedelta(days=int(months * 30.5))).isoformat()
    url = ""
    if path is None and os.path.exists(LOCAL):
        path = LOCAL
        url = "local file " + LOCAL
    if path is None:
        td = workdir or tempfile.mkdtemp()
        path = os.path.join(td, "series.xlsx")
        url = fetch_workbook(path)
        print("appointments file:", url, "-", os.path.getsize(path) // (1 << 20), "MiB")
    appts, info = read_appointments(path, since=since)
    info.update({"file": url, "since": since, "rows": len(appts)})
    print("appointments since", since, ":", len(appts), "| sheet:", info["sheet"], "| columns:", info["columns"])
    return latest_by_acn(appts), info
