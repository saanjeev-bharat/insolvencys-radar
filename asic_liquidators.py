
"""ASIC register of liquidators (open data).

Uses registered liquidator names, firms and locations to add a firm hint
beside an appointee's name. This register does not provide phone or email.
"""

import csv
import io
import os
import re
import tempfile
import zipfile


import asic_companies as C

PACKAGE = "388c5a74-fa9e-4b48-8881-95467e92fe6a"


def _norm(value):
    """Normalise text for reliable comparison."""
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value if value is not None else "").lower(),
    ).strip()


def _pick_resource(resources):
    """Choose the latest available Excel, CSV or ZIP register."""
    candidates = []

    for resource in resources or []:
        url = (resource.get("url") or "").strip()

        if not url:
            continue

        fmt = (resource.get("format") or "").strip().lower()
        clean_url = url.lower().split("?")[0]

        if (
            fmt in ("xlsx", "csv", "zip")
            or clean_url.endswith((".xlsx", ".csv", ".zip"))
        ):
            candidates.append(resource)
            

    if not candidates:
        raise RuntimeError(
            "No Excel, CSV or ZIP liquidator register found"
        )

    def resource_key(resource):
        description = (
            (resource.get("name") or "")
            + " "
            + (resource.get("description") or "")
        ).lower()

        return (
            1 if "current" in description else 0,
            resource.get("last_modified")
            or resource.get("created")
            or "",
        )

    return max(candidates, key=resource_key)["url"]


def read_table(path):
    """Yield dictionary rows from Excel, CSV, TSV or ZIP files."""

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if "xl/workbook.xml" in archive.namelist():
                yield from _xlsx_rows(path)
                return

            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".csv", ".txt", ".tsv"))
                and not name.endswith("/")
            ]

            if not names:
                raise RuntimeError(
                    "ZIP archive contains no supported data file"
                )

            with archive.open(names[0]) as raw:
                yield from _text_rows(raw)

        return

    if path.lower().endswith((".xlsx", ".xls")):
        if path.lower().endswith(".xls"):
            raise RuntimeError(
                "Legacy XLS format is not supported; use XLSX or CSV"
            )

        yield from _xlsx_rows(path)
        return

    with open(path, "rb") as raw:
        yield from _text_rows(raw)


def _text_rows(raw):
    """Read comma-separated or tab-separated text data."""

    text = io.TextIOWrapper(
        raw,
        encoding="utf-8-sig",
        errors="replace",
        newline="",
    )

    try:
        first = text.readline()

        if not first:
            raise RuntimeError("Liquidator data file is empty")

        delimiter = "\t" if "\t" in first else ","

        header = [
            value.strip().strip('"')
            for value in next(
                csv.reader([first], delimiter=delimiter)
            )
        ]

        if not header or not any(header):
            raise RuntimeError("Liquidator data has no column headers")

        for fields in csv.reader(text, delimiter=delimiter):
            if fields:
                yield dict(zip(header, fields))

    finally:
        text.detach()


def _xlsx_rows(path):
    """Read the first worksheet of an Excel workbook."""

    import openpyxl

    workbook = openpyxl.load_workbook(
        path,
        read_only=True,
        data_only=True,
    )

    try:
        if not workbook.worksheets:
            raise RuntimeError("Excel workbook contains no worksheets")

        worksheet = workbook.worksheets[0]

        if hasattr(worksheet, "reset_dimensions"):
            worksheet.reset_dimensions()

        header = None

        for row in worksheet.iter_rows(values_only=True):

            if header is None:
                if row and any(
                    value not in (None, "")
                    for value in row
                ):
                    header = [
                        str(value or "").strip()
                        for value in row
                    ]

                continue

            if not row or not any(
                value not in (None, "")
                for value in row
            ):
                continue

            yield dict(zip(header, row))

        if header is None:
            raise RuntimeError("Excel workbook contains no header row")

    finally:
        workbook.close()


def _pick(row, *parts, avoid=()):
    """Find a column whose normalised name contains the requested terms."""

    for key, value in row.items():
        name = _norm(key)

        if (
            all(part in name for part in parts)
            and not any(term in name for term in avoid)
        ):
            return str(
                value if value is not None else ""
            ).strip()

    return ""


def tokens(name):
    """Return meaningful name tokens."""
    return {
        token
        for token in _norm(name).split()
        if len(token) > 1
    }


def read_directory(path):
    """Read active liquidator records and remove duplicate entries."""

    people = []
    seen = set()

    for row in read_table(path):

        name = (
            _pick(row, "liquidator", "name")
            or _pick(row, "name", avoid=("firm",))
        )

        if not name:
            continue

        status = _pick(
            row,
            "status",
            avoid=("suspension",),
        ).lower()

        inactive_terms = (
            "ceased",
            "cancel",
            "deceased",
            "removed",
            "resign",
        )

        if any(term in status for term in inactive_terms):
            continue

        number = _pick(row, "number")
        key = (_norm(name), number)

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


def match(appointee, people):
    """Return one unambiguous registered liquidator match, or None."""

    appointee_tokens = tokens(appointee)

    if len(appointee_tokens) < 2:
        return None

    matches = []

    for person in people:
        person_tokens = person["tokens"]

        if (
            len(appointee_tokens & person_tokens) >= 2
            and (
                appointee_tokens <= person_tokens
                or person_tokens <= appointee_tokens
            )
        ):
            matches.append(person)

    return matches[0] if len(matches) == 1 else None


def load(path=None, workdir=None):
    """Load the ASIC liquidator register and return (people, source_url)."""

    url = ""

    if path is not None:
        people = read_directory(path)

    elif workdir:
        os.makedirs(workdir, exist_ok=True)

        url = _pick_resource(C.fetch_listing(PACKAGE))

        path = os.path.join(workdir, "liquidators.dat")

        C.download(url, path)
        people = read_directory(path)

    else:
        with tempfile.TemporaryDirectory() as temp_dir:

            url = _pick_resource(C.fetch_listing(PACKAGE))

            path = os.path.join(
                temp_dir,
                "liquidators.dat",
            )

            C.download(url, path)
            people = read_directory(path)

    print(
        "Liquidator register:",
        len(people),
        "people |",
        url,
    )

    return people, url
