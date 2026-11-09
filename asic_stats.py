
"""
ASIC insolvency statistics, Series 1 and 2.

One record per external administration or controller appointment.
Includes company, ACN, appointee, appointment type and date,
industry, state, principal place of business and postcode.

The workbook's file name changes every week, so the download link
is read from ASIC's page.

The latest workbook is downloaded first. If ASIC's site refuses
the download, a repository copy named asic_series.xlsx is used.
"""

import datetime
import os
import re
import tempfile
from urllib.parse import urljoin

import asic_companies as C


PAGE = (
    "https://asic.gov.au/regulatory-resources/find-a-document/statistics/"
    "insolvency-statistics/insolvency-statistics-series-1-companies-entering-external-administration/"
)

LOCAL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "asic_series.xlsx"
)


def find_xlsx_url(html, base=PAGE):
    links = re.findall(
        r'href=["\']([^"\']+\.xlsx(?:\?[^"\']*)?)["\']',
        html,
        flags=re.I
    )

    if not links:
        raise RuntimeError(
            "Could not find an Excel workbook link on ASIC's statistics page"
        )

    preferred = [
        link for link in links
        if "series" in link.lower()
    ]

    selected = preferred[0] if preferred else links[0]

    return urljoin(base, selected.replace("&amp;", "&"))


def fetch_workbook(dest):
    """Download the latest ASIC workbook. Returns the URL used."""
    import httpx

    response = httpx.get(
        PAGE,
        headers={"User-Agent": C.UA},
        timeout=60,
        follow_redirects=True
    )
    response.raise_for_status()

    url = find_xlsx_url(response.text)
    C.download(url, dest)

    # Validate that the downloaded file is a readable Excel workbook.
    import openpyxl

    wb = openpyxl.load_workbook(
        dest,
        read_only=True,
        data_only=True
    )
    wb.close()

    return url


def _norm(s):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(s if s is not None else "").lower()
    ).strip()


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

        "name": _find(
            h,
            [
                "organisation name",
                "company name",
                "organization name",
                "entity name",
                "name"
            ],
            avoid=["appointee", "industry", "anzsic"]
        ),

        "appointee": _find(
            h,
            ["appointee name", "appointee"],
            avoid=["acn"]
        ),

        "atype": _find(
            h,
            ["appointment type", "type of appointment", "appointment"],
            avoid=["date", "financial", "appointee"]
        ),

        "adate": _find(
            h,
            [
                "appointment start date",
                "start date",
                "appointment date",
                "date of appointment",
                "date"
            ],
            avoid=["financial", "year", "end"]
        ),

        "area": _find(
            h,
            ["area", "suburb", "locality", "city", "town"],
            avoid=["industry"]
        ),

        "postcode": _find(h, ["postcode", "post code"]),
    }

    ind = None

    for i, x in enumerate(h):
        if "division" in x and any(
            k in x for k in ("desc", "name", "title")
        ):
            ind = i
            break

    if ind is None:
        ind = _find(
            h,
            ["anzsic division", "division", "industry"],
            avoid=["code"]
        )

    m["industry"] = ind

    st = None

    for i, x in enumerate(h):
        if "state" in x and any(
            k in x for k in ("business", "location", "principal", "place")
        ):
            st = i
            break

    if st is None:
        st = _find(h, ["state"], avoid=["incorporation"])

    m["state"] = st

    m["state_inc"] = _find(
        h,
        ["state of incorporation", "incorporation"]
    )

    return m


def _date(v):
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()

    if isinstance(v, datetime.date):
        return v.isoformat()

    s = str(v or "").strip()

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d %H:%M:%S"
    ):
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
    """Normalise ASIC state names to website filter abbreviations."""
    s = str(v or "").strip().upper().replace(".", "")

    aliases = {
        "NEW SOUTH WALES": "NSW",
        "NSW": "NSW",
        "VICTORIA": "VIC",
        "VIC": "VIC",
        "QUEENSLAND": "QLD",
        "QLD": "QLD",
        "WESTERN AUSTRALIA": "WA",
        "WA": "WA",
        "SOUTH AUSTRALIA": "SA",
        "SA": "SA",
        "TASMANIA": "TAS",
        "TAS": "TAS",
        "AUSTRALIAN CAPITAL TERRITORY": "ACT",
        "ACT": "ACT",
        "NORTHERN TERRITORY": "NT",
        "NT": "NT",
    }

    return aliases.get(s, "")


def read_appointments(path, since=None):
    """Read appointment records from the ASIC Excel workbook."""
    import openpyxl

    wb = openpyxl.load_workbook(
        path,
        read_only=True,
        data_only=True
    )

    tried = []

    for ws in reversed(wb.worksheets):
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()

        header = None
        mapping = None
        hidx = None

        rows = ws.iter_rows(values_only=True)

        for i, row in enumerate(rows):
            if i > 40:
                break

            cand = map_columns(row)

            if (
                cand["acn"] is not None
                and cand["name"] is not None
                and (
                    cand["atype"] is not None
                    or cand["adate"] is not None
                )
            ):
                header = row
                mapping = cand
                hidx = i
                break

        tried.append(ws.title)

        if mapping is None:
            continue

        out = []

        def get_value(row, key):
            index = mapping.get(key)

            if index is not None and index < len(row):
                return row[index]

            return None

        for row in rows:
            acn = re.sub(
                r"\D",
                "",
                str(get_value(row, "acn") or "")
            )

            if len(acn) < 8:
                continue

            d = _date(get_value(row, "adate"))

            if since and d and d < since:
                continue

            atype = str(
                get_value(row, "atype") or ""
            ).strip()

            area = str(
                get_value(row, "area") or ""
            ).strip().title()

            state = (
                _state(get_value(row, "state"))
                or _state(get_value(row, "state_inc"))
            )

            # ASIC occasionally labels ACT-region records as AUS.
            # Use the area only when it explicitly identifies the ACT.
            if (
                not state
                and "AUSTRALIAN CAPITAL TERRITORY" in area.upper()
            ):
                state = "ACT"

            out.append({
                "acn": acn.zfill(9),
                "name": str(
                    get_value(row, "name") or ""
                ).strip(),
                "appointee": str(
                    get_value(row, "appointee") or ""
                ).strip(),
                "atype": atype,
                "cat": category(atype),
                "date": d,
                "industry": str(
                    get_value(row, "industry") or ""
                ).strip(),
                "state": state,
                "area": area,
                "postcode": re.sub(
                    r"\D",
                    "",
                    str(get_value(row, "postcode") or "")
                )[:4],
            })

        info = {
            "sheet": ws.title,
            "header_row": hidx + 1,
            "columns": {
                k: (
                    str(header[v]).strip()
                    if v is not None
                    else None
                )
                for k, v in mapping.items()
            }
        }

        wb.close()
        return out, info

    wb.close()

    raise RuntimeError(
        "could not find the raw appointment data in the workbook "
        "(sheets checked: %s)" % ", ".join(tried)
    )


def latest_by_acn(appts):
    """Return newest appointment per company and appointment count."""
    best = {}
    count = {}

    for appointment in appts:
        acn = appointment["acn"]

        count[acn] = count.get(acn, 0) + 1

        current = best.get(acn)

        if (
            current is None
            or (appointment["date"] or "") > (current["date"] or "")
        ):
            best[acn] = appointment

    for acn, appointment in best.items():
        appointment["count"] = count[acn]

    return best


def load(months=12, path=None, workdir=None):
    """Download the latest ASIC workbook, falling back to a local copy."""

    since = (
        datetime.date.today()
        - datetime.timedelta(days=int(months * 30.5))
    ).isoformat()

    url = ""

    # An explicitly supplied workbook takes priority.
    if path is None:
        # Always attempt to download the latest workbook first.
        td = workdir or tempfile.mkdtemp()
        os.makedirs(td, exist_ok=True)

        path = os.path.join(td, "series.xlsx")

        try:
            url = fetch_workbook(path)
            print("Downloaded ASIC workbook:", url)

        except Exception as exc:
            print("ASIC download failed:", exc)

            # Use the repository copy only if the download fails.
            if os.path.exists(LOCAL):
                path = LOCAL
                url = "local fallback file " + LOCAL
                print("Using local ASIC workbook:", LOCAL)

            else:
                raise RuntimeError(
                    "ASIC download failed and asic_series.xlsx "
                    "was not found in the repository."
                ) from exc

    appts, info = read_appointments(path, since=since)

    info.update({
        "file": url or path,
        "since": since,
        "rows": len(appts)
    })

    print(
        "ASIC appointments since",
        since,
        ":",
        len(appts),
        "| sheet:",
        info["sheet"],
        "| columns:",
        info["columns"]
    )

    return latest_by_acn(appts), info
