
"""Source adapters for Insolvency Radar.

Each source yields dictionaries containing:
    source, source_ref, text, kind

Use official datasets and approved feeds wherever possible.
Do not enable automated website collection without checking the
relevant site's access terms and obtaining any required permission.
"""


import os
import time
from typing import Iterator
from urllib.parse import urljoin, urlparse


def _env_float(name, default, minimum=0.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a valid number")

    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")

    return value


def _env_int(name, default, minimum=0, maximum=5):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}"
        )

    return value


UA = os.environ.get(
    "RADAR_USER_AGENT",
    "InsolvencyRadar/1.0",
).strip()

DELAY = _env_float("RADAR_DELAY", "2.0")
MAX_RETRIES = _env_int("RADAR_MAX_RETRIES", "3")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _get(url: str, **params) -> str:
    """Fetch a page with a timeout and limited retries."""

    import httpx

    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        if DELAY:
            time.sleep(DELAY)

        try:
            response = httpx.get(
                url,
                params=params,
                headers={"User-Agent": UA},
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )

            if response.status_code in RETRYABLE_STATUS_CODES:
                response.raise_for_status()

            response.raise_for_status()
            return response.text

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc

        except httpx.HTTPStatusError as exc:
            last_error = exc

            if exc.response.status_code not in RETRYABLE_STATUS_CODES:
                raise

            retry_after = exc.response.headers.get("Retry-After")

            if retry_after:
                try:
                    wait = min(float(retry_after), 30.0)
                except ValueError:
                    wait = min(2 ** attempt, 30.0)
            else:
                wait = min(2 ** attempt, 30.0)

            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue

            raise

        if attempt < MAX_RETRIES:
            time.sleep(min(2 ** attempt, 30.0))

    raise RuntimeError(
        f"Request failed after {MAX_RETRIES + 1} attempts: {url}"
    ) from last_error


def _absolute_url(base, href):
    """Resolve a listing link safely."""
    if not href:
        return base

    result = urljoin(base, href)

    if urlparse(result).scheme not in ("http", "https"):
        return base

    return result


class Source:
    name = "base"
    kind = "insolvency"

    def fetch(self) -> Iterator[dict]:
        raise NotImplementedError


class AsicNotices(Source):
    """ASIC Published Notices adapter.

    Automatic HTML collection is intentionally disabled until the
    access conditions, supported interface and live page structure
    have been verified.
    """

    name = "asic_notices"
    kind = "insolvency"

    def fetch(self):
        raise RuntimeError(
            "ASIC Published Notices automated collection is disabled "
            "pending verification of authorised access and the live "
            "search interface."
        )


class AuctionSite(Source):
    """Configurable adapter for an approved auction listing page."""

    kind = "auction"

    def __init__(
        self,
        name,
        url,
        item_selector,
        link_selector="a",
        min_text_length=40,
    ):
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Auction source requires a valid HTTP(S) URL")

        if not name or not item_selector:
            raise ValueError("Source name and item selector are required")

        if min_text_length < 1:
            raise ValueError("min_text_length must be positive")

        self.name = name
        self.url = url
        self.item_selector = item_selector
        self.link_selector = link_selector
        self.min_text_length = min_text_length

    def fetch(self):
        from bs4 import BeautifulSoup

        html = _get(self.url)
        soup = BeautifulSoup(html, "html.parser")

        seen = set()

        for item in soup.select(self.item_selector):
            text = item.get_text(" ", strip=True)

            if len(text) < self.min_text_length:
                continue

            link = item.select_one(self.link_selector)

            href = (
                link.get("href")
                if link and link.get("href")
                else None
            )

            item_url = _absolute_url(self.url, href)

            if item_url in seen:
                continue

            seen.add(item_url)

            yield {
                "source": self.name,
                "source_ref": item_url,
                "source_url": item_url,
                "text": text,
                "kind": self.kind,
            }


class MortgageeListings(AuctionSite):
    kind = "mortgagee_sale"


class FileSource(Source):
    """Offline/testing source: one notice per blank-line-separated block."""

    def __init__(self, path, kind="insolvency"):
        self.path = path
        self.kind = kind
        self.name = f"file:{os.path.basename(path)}"

    def fetch(self):
        with open(self.path, encoding="utf-8") as file:
            content = file.read()

        blocks = content.split("\n\n")

        for index, block in enumerate(blocks):
            text = block.strip()

            if not text:
                continue

            yield {
                "source": self.name,
                "source_ref": str(index),
                "source_url": None,
                "text": text,
                "kind": self.kind,
            }


def default_sources() -> list[Source]:
    """Return approved, configured live sources."""

    # ASIC Published Notices is deliberately not enabled here.
    # Add approved and verified sources only after their access
    # conditions and live selectors have been checked.

    return []
