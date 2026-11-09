
"""Weekly job: gather Australia-wide insolvency data from ASIC's open
sources and write leads.json.

Sources:
  1. ASIC insolvency statistics, Series 1 and 2.
  2. ASIC Company Dataset (data.gov.au).
  3. ASIC register of liquidators (data.gov.au).
"""

import datetime
import json
import math
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
    """Load first-seen dates from a compatible previous output."""

    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {}, None

        meta = data.get("meta")
        records = data.get("leads")

        if not isinstance(meta, dict) or not isinstance(records, list):
            return {}, None

        if meta.get("sample") or meta.get("kind") not in OLD_KINDS:
            return {}, None

        previous = {}

        for record in records:
            if not isinstance(record, dict):
                continue

            acn = record.get("a")
            if acn:
                previous[str(acn)] = record.get("f")

        return previous, meta.get("baseline")

    except (OSError, ValueError, TypeError):
        return {}, None


def build_leads(latest, companies, people, previous, today):
    """Combine appointments and company status into one record per ACN."""

    leads = {}

    for acn, appointment in latest.items():
        if not isinstance(appointment, dict):
            continue

        acn = str(acn).strip()
        if not acn:
            continue

        company = companies.get(acn, {})

        if not isinstance(company, dict):
            company = {}

        appointee = appointment.get("appointee") or ""

        record = {
            "n": appointment.get("name") or company.get("n", ""),
            "a": acn,
            "b": company.get("b", ""),
            "s": company.get("s", ""),
            "t": company.get("t", ""),
            "c": company.get("c", ""),
            "r": company.get("r", ""),
            "k": appointment.get("cat", ""),
            "p": appointment.get("atype", ""),
            "d": appointment.get("date", ""),
            "ap": appointee,
            "st": appointment.get("state", ""),
            "ar": appointment.get("area", ""),
            "pc": appointment.get("postcode", ""),
            "in": appointment.get("industry", ""),
            "na": appointment.get("count", 1),
        }

        hit = (
            L.match(appointee, people)
            if appointee and people
            else None
        )

        if hit:
            record["fm"] = hit.get("firm", "")
            record["pt"] = ", ".join(
                value
                for value in (
                    hit.get("town", ""),
                    hit.get("state", ""),
                )
                if value
            )

        leads[acn] = record

    for acn, company in companies.items():
        acn = str(acn).strip()

        if not acn or acn in leads:
            continue

        if not isinstance(company, dict):
            continue

        status = company.get("s")

        if status not in C.WANTED:
            continue

        leads[acn] = {
            "n": company.get("n", ""),
            "a": acn,
            "b": company.get("b", ""),
            "s": status,
            "t": company.get("t", ""),
            "c": company.get("c", ""),
            "r": company.get("r", ""),
            "k": "E" if status == "EXAD" else "S",
        }

    output = []

    for acn, record in leads.items():
        record = {
            key: value
            for key, value in record.items()
            if value not in ("", None)
        }

        record["f"] = previous.get(acn) or today
        output.append(record)

    # Primary: newest appointment date.
    # Secondary: newest first-seen date.
    # Tertiary: company name alphabetically.
    output.sort(key=lambda row: row.get("n", "").lower())
    output.sort(key=lambda row: row.get("f", ""), reverse=True)
    output.sort(key=lambda row: row.get("d", ""), reverse=True)

    return output


def _get_months(value=None):
    """Read and validate the appointment look-back period."""

    if value is None:
        value = os.environ.get("RADAR_MONTHS", "12")

    try:
        months = float(value)
    except (TypeError, ValueError):
        raise ValueError("RADAR_MONTHS must be a valid number")

    if not math.isfinite(months) or not 0 < months <= 120:
        raise ValueError("RADAR_MONTHS must be greater than 0 and at most 120")

    return months


def _atomic_json_write(path, data):
    """Write JSON safely, replacing the destination only after success."""

    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))

    os.makedirs(directory, exist_ok=True)

    fd, temporary_path = tempfile.mkstemp(
        prefix=".leads-",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_path, path)

    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def run(out="leads.json", months=None, paths=None):
    """Collect available ASIC data and produce the combined lead file."""

    paths = paths or {}
    months = _get_months(months)

    today = datetime.datetime.now(
        datetime.timezone.utc
    ).date().isoformat()

    previous, baseline = load_previous(out)

    sources = {}
    info = {}
    latest = {}
    companies = {}
    counts = Counter()
    people = []

    # All downloaded source files are kept in a temporary directory
    # and removed automatically after collection.
    with tempfile.TemporaryDirectory(prefix="insolvency-radar-") as td:

        try:
            latest, appointment_info = S.load(
                months=months,
                path=paths.get("stats"),
                workdir=td,
            )

            if not isinstance(latest, dict):
                raise ValueError("ASIC appointments returned invalid data")

            if not isinstance(appointment_info, dict):
                appointment_info = {}

            info["appointments"] = appointment_info
            sources["appointments"] = "ok"

        except Exception as error:
            sources["appointments"] = "failed: %s" % error
            print("WARNING appointments:", error, file=sys.stderr)

        try:
            path = paths.get("companies")

            if path is None:
                url = (
                    os.environ.get("RADAR_COMPANY_URL")
                    or C.pick_resource(C.fetch_listing())
                )

                if not url:
                    raise RuntimeError(
                        "Could not locate the ASIC company dataset"
                    )

                print("company dataset file:", url)

                path = os.path.join(td, "company.dat")
                C.download(url, path)
                info["company_file"] = url

            companies, counts = C.collect(
                C.iter_rows(path),
                extra=set(latest),
            )

            if not isinstance(companies, dict):
                raise ValueError("ASIC company dataset returned invalid data")

            if not isinstance(counts, Counter):
                counts = Counter(counts or {})

            sources["company_status"] = "ok"

        except Exception as error:
            sources["company_status"] = "failed: %s" % error
            print("WARNING company dataset:", error, file=sys.stderr)

        try:
            people, liquidator_url = L.load(
                path=paths.get("liquidators"),
                workdir=td,
            )

            if not isinstance(people, list):
                raise ValueError("ASIC liquidator register returned invalid data")

            info["liquidator_file"] = liquidator_url
            sources["liquidator_register"] = "ok"

        except Exception as error:
            sources["liquidator_register"] = "failed: %s" % error
            print("WARNING liquidator register:", error, file=sys.stderr)

        leads = build_leads(
            latest,
            companies,
            people,
            previous,
            today,
        )

    if not leads:
        raise RuntimeError(
            "No data collected from any source: %s" % sources
        )

    if sources["appointments"] != "ok":
        print(
            "NOTE: appointment data unavailable; "
            "showing company status data where available",
            file=sys.stderr,
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
        "months": months,
        "sources": sources,
        "by_category": dict(
            Counter(row.get("k", "?") for row in leads)
        ),
        "by_state": dict(
            Counter(
                row["st"]
                for row in leads
                if row.get("st")
            )
        ),
        "with_practitioner": sum(
            1 for row in leads if row.get("ap")
        ),
        "with_firm": sum(
            1 for row in leads if row.get("fm")
        ),
        "files": {
            key: value
            for key, value in info.items()
            if isinstance(value, str)
        },
        "columns_used": (
            info.get("appointments") or {}
        ).get("columns"),
        "company_status_counts": dict(
            counts.most_common(12)
        ),
    }

    _atomic_json_write(
        out,
        {
            "meta": meta,
            "leads": leads,
        },
    )

    print("sources:", sources)
    print(
        "leads:", len(leads),
        "| with practitioner:", meta["with_practitioner"],
        "| with firm:", meta["with_firm"],
    )

    return len(leads)


if __name__ == "__main__":
    try:
        print(f"wrote {run()} companies")
    except Exception as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
