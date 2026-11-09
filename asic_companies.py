
"""Companies in external administration or strike-off, from ASIC's open Company Dataset.

Source: ASIC Company Dataset on data.gov.au.
Licence: CC BY 3.0 AU.

Status codes:
    EXAD - External administration
    SOFF - Strike-off action in progress
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
from contextlib import ExitStack

PACKAGE_ID = "7b8656f9-606d-4337-af29-66b89b2eeefb"
API = f"https://data.gov.au/data/api/3/action/package_show?id={PACKAGE_ID}"

WANTED = ("EXAD", "SOFF")
UA = os.environ.get(
    "RADAR_USER_AGENT",
    "InsolvencyRadar/0.2 (github-actions)",
)
KIND = "asic_company_dataset"


# ---------- Finding and downloading the file ----------

def pick_resource(resources):
    """Choose the most recent CSV or ZIP Company Dataset resource."""
    candidates = []

    for resource in resources or []:
        url = (resource.get("url") or "").strip()

        if not url:
            continue

        fmt = (resource.get("format") or "").strip().lower()
        clean_url = url.lower().split("?")[0]

        if fmt in ("csv", "zip") or clean_url.endswith((".csv", ".zip")):
            candidates.append(resource)

    if not candidates:
        raise RuntimeError(
            "No CSV or ZIP file found in the dataset listing"
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

    selected = max(candidates, key=resource_key)
    return selected["url"]


def fetch_listing(package_id=PACKAGE_ID):
    import httpx

    api = (
        "https://data.gov.au/data/api/3/action/package_show"
        f"?id={package_id}"
    )

    response = httpx.get(
        api,
        headers={"User-Agent": UA},
        timeout=60,
        follow_redirects=True,
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        raise RuntimeError(
            "data.gov.au did not return the dataset listing"
        )

    return data["result"]["resources"]


def download(url, dest):
    import httpx

    last_error = None

    for attempt in range(3):
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"User-Agent": UA},
                timeout=httpx.Timeout(60.0, read=600.0),
                follow_redirects=True,
            ) as response:

                response.raise_for_status()

                with open(dest, "wb") as output:
                    for chunk in response.iter_bytes(1 << 20):
                        output.write(chunk)

            if os.path.getsize(dest) == 0:
                raise RuntimeError("Downloaded dataset is empty")

            return

        except Exception as error:
            last_error = error

            if os.path.exists(dest):
                os.remove(dest)

            time.sleep(5 * (attempt + 1))

    raise RuntimeError(f"Download failed: {last_error}")


# ---------- Reading the file ----------

def iter_rows(path):
    """Read CSV, TSV or ZIP datasets and yield dictionary rows."""

    with ExitStack() as stack:

        if zipfile.is_zipfile(path):
            archive = stack.enter_context(zipfile.ZipFile(path))

            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".csv", ".txt", ".tsv"))
                and not name.endswith("/")
            ]

            if not names:
                raise RuntimeError(
                    "ZIP archive contains no CSV, TXT or TSV file"
                )

            raw = stack.enter_context(archive.open(names[0]))

        else:
            raw = stack.enter_context(open(path, "rb"))

        text = stack.enter_context(
            io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
                errors="replace",
                newline="",
            )
        )

        first = text.readline()

        if not first:
            raise RuntimeError("Dataset file is empty")

        tab_separated = "\t" in first

        options = (
            {
                "delimiter": "\t",
                "quoting": csv.QUOTE_NONE,
            }
            if tab_separated
            else {"delimiter": ","}
        )

        header = [
            value.strip().strip('"')
            for value in next(csv.reader([first], **options))
        ]

        if not header:
            raise RuntimeError("Dataset has no column headers")

        for fields in csv.reader(text, **options):
            if fields:
                yield dict(zip(header, fields))


# ---------- Turning rows into records ----------

def _clean(value):
    return (value or "").strip().strip('"').strip()


def _iso_date(value):
    value = _clean(value)

    for fmt in (
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%y",
    ):
        try:
            return datetime.datetime.strptime(
                value, fmt
            ).date().isoformat()

        except ValueError:
            continue

    return ""


def collect(rows, extra=None):
    """Return company records indexed by ACN and dataset status counts.

    Includes EXAD and SOFF companies, plus explicitly supplied ACNs
    regardless of their current status.
    """

    extra = {
        digits.zfill(9)
        for value in (extra or ())
        if (digits := re.sub(r"\D", "", str(value)))
    }

    found = {}
    counts = Counter()

    for row in rows:

        code = _clean(row.get("Status")).upper()
        counts[code] += 1

        if _clean(
            row.get("Current Name Indicator")
        ).upper() == "N":
            continue

        acn = re.sub(
            r"\D",
            "",
            row.get("ACN") or "",
        )

        if not acn:
            continue

        acn = acn.zfill(9)

        if code not in WANTED and acn not in extra:
            continue

        if acn in found:
            continue

        abn = re.sub(
            r"\D",
            "",
            row.get("ABN") or "",
        )

        found[acn] = {
            "n": _clean(row.get("Company Name")),
            "a": acn,
            "b": abn if len(abn) == 11 else "",
            "s": code,
            "t": _clean(row.get("Type")),
            "c": _clean(row.get("Class")),
            "r": _iso_date(
                row.get("Date of Registration")
            ),
        }

    return found, counts


# ---------- Preserving previous records ----------

def load_previous(path):
    """Preserve first-seen dates and the original baseline date."""

    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)

    except (OSError, ValueError, TypeError):
        return {}, None

    meta = data.get("meta") or {}

    if meta.get("sample") or meta.get("kind") != KIND:
        return {}, None

    previous = {
        record["a"]: record.get("f")
        for record in data.get("leads", [])
        if record.get("a")
    }

    return previous, meta.get("baseline")


# ---------- Building the output ----------

def build(found, counts, previous, baseline, today, source_url=""):
    """Build the final company dataset and metadata."""

    previous = previous or {}
    counts = counts or {}

    leads = []

    for acn, record in found.items():

        item = dict(record)

        item["a"] = item.get("a") or acn
        item["n"] = item.get("n") or "Unknown company"
        item["f"] = previous.get(acn) or today

        leads.append(item)

    # Newest first, then alphabetical by company name.
    leads.sort(
        key=lambda item: (
            item.get("n") or ""
        ).casefold()
    )

    leads.sort(
        key=lambda item: item.get("f") or "",
        reverse=True,
    )

    meta = {
        "kind": KIND,
        "sample": False,
        "updated": time.strftime(
            "%Y-%m-%d %H:%M UTC",
            time.gmtime(),
        ),
        "baseline": baseline or today,
        "count": len(leads),
        "by_status": dict(
            Counter(
                item.get("s", "")
                for item in leads
            )
        ),
        "all_status_counts": dict(counts),
        "dataset_file": source_url,
    }

    return {
        "meta": meta,
        "leads": leads,
    }


# ---------- Main execution ----------

def run(out="leads.json", url=None, path=None):
    """Download, process and save the latest ASIC company dataset."""

    today = datetime.date.today().isoformat()

    previous, baseline = load_previous(out)

    with tempfile.TemporaryDirectory() as temp_dir:

        if path is None:

            url = (
                url
                or os.environ.get("RADAR_COMPANY_URL")
                or pick_resource(fetch_listing())
            )

            print("Dataset file:", url)

            path = os.path.join(
                temp_dir,
                "company.dat",
            )

            download(url, path)

            print(
                "Downloaded",
                os.path.getsize(path) // (1 << 20),
                "MiB",
            )

        found, counts = collect(iter_rows(path))

    print(
        "Status counts in file:",
        dict(counts.most_common(12)),
    )

    if not found:
        raise RuntimeError(
            "No companies with status EXAD or SOFF were found. "
            "The file format may have changed. "
            f"Status counts: {dict(counts)}"
        )

    data = build(
        found,
        counts,
        previous,
        baseline,
        today,
        url or "",
    )

    output_path = os.path.abspath(out)
    output_dir = os.path.dirname(output_path)

    os.makedirs(output_dir, exist_ok=True)

    temp_path = output_path + ".tmp"

    try:
        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        os.replace(temp_path, output_path)

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    print(
        "Companies by status:",
        data["meta"]["by_status"],
    )

    return len(data["leads"])


if __name__ == "__main__":

    try:
        print(f"Wrote {run()} companies")

    except Exception as error:
        print(
            "ERROR:",
            error,
            file=sys.stderr,
        )
        sys.exit(1)
