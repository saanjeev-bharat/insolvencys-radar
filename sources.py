"""Source adapters. Each yields dicts with at least: source, source_ref, text.

All sources are public. Check each site's terms of use and robots.txt before
scheduled scraping, and prefer official feeds/APIs/bulk datasets where offered.
Selectors below are starting points: site markup changes, so verify them.

  ASIC Published Notices   https://publishednotices.asic.gov.au   (liquidator/administrator/receiver appointments)
  Auction houses           Pickles, Grays, Lawsons, etc.           (insolvency & bank-repossession asset sales)
  Mortgagee listings       realestate.com.au / domain.com.au       (agent listings tagged 'mortgagee')
"""
import os
import time
from typing import Iterator


UA = os.environ.get("RADAR_USER_AGENT", "InsolvencyRadar/0.1 (contact: set-your-email)")
DELAY = float(os.environ.get("RADAR_DELAY", "2.0"))  # be polite


def _get(url: str, **params) -> str:
    import httpx  # lazy: only needed for live sources
    time.sleep(DELAY)
    r = httpx.get(url, params=params, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


class Source:
    name = "base"
    kind = "insolvency"

    def fetch(self) -> Iterator[dict]:  # pragma: no cover
        raise NotImplementedError


class AsicNotices(Source):
    """ASIC published notices (external administration / winding up)."""
    name = "asic_notices"
    kind = "insolvency"
    BASE = "https://publishednotices.asic.gov.au"

    def __init__(self, query_path="/browse", pages=5):
        self.query_path, self.pages = query_path, pages

    def fetch(self):
        import httpx
        from bs4 import BeautifulSoup
        for page in range(1, self.pages + 1):
            html = _get(self.BASE + self.query_path, page=page)
            soup = BeautifulSoup(html, "html.parser")
            for item in soup.select("article, .notice, tr"):  # adjust to live markup
                text = item.get_text(" ", strip=True)
                if len(text) < 80:
                    continue
                a = item.find("a", href=True)
                url = httpx.URL(self.BASE).join(a["href"]).human_repr() if a else self.BASE
                yield {"source": self.name, "source_ref": url + str(hash(text) % 10**8),
                       "source_url": url, "text": text, "kind": self.kind}


class AuctionSite(Source):
    """Generic insolvency-auction listing page. Configure per site."""
    kind = "auction"

    def __init__(self, name, url, item_selector, link_selector="a"):
        self.name, self.url = name, url
        self.item_selector, self.link_selector = item_selector, link_selector

    def fetch(self):
        import httpx
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(_get(self.url), "html.parser")
        for item in soup.select(self.item_selector):
            text = item.get_text(" ", strip=True)
            a = item.select_one(self.link_selector)
            url = httpx.URL(self.url).join(a["href"]).human_repr() if a and a.get("href") else self.url
            yield {"source": self.name, "source_ref": url, "source_url": url,
                   "text": text, "kind": self.kind}


class MortgageeListings(AuctionSite):
    kind = "mortgagee_sale"


class FileSource(Source):
    """Offline/testing source: one notice per blank-line-separated block in a text file."""
    def __init__(self, path, kind="insolvency"):
        self.path, self.kind, self.name = path, kind, f"file:{os.path.basename(path)}"

    def fetch(self):
        blocks = open(self.path, encoding="utf-8").read().split("\n\n")
        for i, b in enumerate(x.strip() for x in blocks):
            if b:
                yield {"source": self.name, "source_ref": str(i), "source_url": None,
                       "text": b, "kind": self.kind}


def default_sources() -> list[Source]:
    return [
        AsicNotices(),
        # Examples - confirm URLs/selectors against the live sites:
        # AuctionSite("pickles", "https://www.pickles.com.au/...", ".listing-card"),
        # AuctionSite("grays", "https://www.grays.com/...", ".lot"),
        # MortgageeListings("mortgagee_domain", "https://www.domain.com.au/...", "[data-testid=listing-card]"),
    ]
