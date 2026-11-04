"""ASIC register of liquidators (open data): name, firm and town for registered liquidators.
Used only to add a firm hint next to an appointee's name. It has no phone or email.
"""
import csv
import io
import os
import re
import tempfile
import zipfile

import asic_companies as C

PACKAGE = "388c5a74-fa9e-4b48-8881-95467e92fe6a"


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s if s is not None else "").lower()).strip()


def read_table(path):
    """Yield dict rows from .xlsx, .csv/.tsv (optionally zipped). Header is the first non-empty row."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        if "xl/workbook.xml" in zf.namelist():
            yield from _xlsx_rows(path)
            return
        names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt", ".tsv"))] or zf.namelist()
        raw = zf.open(names[0])
    else:
        raw = open(path, "rb")
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
    try:
        first = text.readline()
        delim = "\t" if "\t" in first else ","
        header = [h.strip() for h in next(csv.reader([first], delimiter=delim))]
        for fields in csv.reader(text, delimiter=delim):
            if fields:
                yield dict(zip(header, fields))
    finally:
        text.close()


def _xlsx_rows(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    if hasattr(ws, "reset_dimensions"):
        ws.reset_dimensions()
    header = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            if row and any(c not in (None, "") for c in row):
                header = [str(c or "").strip() for c in row]
            continue
        yield dict(zip(header, row))
    wb.close()


def _pick(row, *parts, avoid=()):
    for k, v in row.items():
        n = _norm(k)
        if all(p in n for p in parts) and not any(a in n for a in avoid):
            return str(v if v is not None else "").strip()
    return ""


def read_directory(path):
    people, seen = [], set()
    for row in read_table(path):
        name = _pick(row, "liquidator", "name") or _pick(row, "name")
        if not name:
            continue
        status = _pick(row, "status", avoid=("suspension",)).lower()
        if any(w in status for w in ("ceased", "cancel", "deceased", "removed", "resign")):
            continue
        key = (_norm(name), _pick(row, "number"))
        if key in seen:
            continue
        seen.add(key)
        people.append({
            "name": name,
            "tokens": tokens(name),
            "firm": _pick(row, "firm"),
            "town": _pick(row, "town").title(),
            "state": _pick(row, "state").upper(),
        })
    return people


def tokens(name):
    return {t for t in _norm(name).split() if len(t) > 1}


def match(appointee, people):
    """Return the one registered liquidator that fits this name, or None if none or several fit."""
    a = tokens(appointee)
    if len(a) < 2:
        return None
    hits = [p for p in people if len(a & p["tokens"]) >= 2 and (a <= p["tokens"] or p["tokens"] <= a)]
    return hits[0] if len(hits) == 1 else None


def load(path=None, workdir=None):
    url = ""
    if path is None:
        url = C.pick_resource(C.fetch_listing(PACKAGE))
        path = os.path.join(workdir or tempfile.mkdtemp(), "liquidators.dat")
        C.download(url, path)
    people = read_directory(path)
    print("liquidator register:", len(people), "people", "|", url)
    return people, url
