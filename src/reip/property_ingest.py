"""Listing-link ingestion — paste a Redfin/Zillow/Realtor URL → property dict.

The spec's POST /api/properties/ingest. Three strategies, in order:
  1. Source-specific JSON API (Redfin only — stingray initialInfo).
  2. JSON-LD <script type="application/ld+json"> in the page HTML.
  3. Regex fallback on visible HTML (price, beds, baths, sqft).

Whatever fields don't extract come back as None; the UI flags them and
asks the user to fill manually. ZORI is then used as a rent fallback
when the listing doesn't carry a rent estimate.

No paid keys. No headless browser. Best-effort scraping that fails open.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse
import httpx


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


@dataclass
class IngestedProperty:
    source: str
    url: str
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    listed_price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    year_built: Optional[int] = None
    lot_size: Optional[float] = None
    days_on_market: Optional[int] = None
    rent_estimate: Optional[float] = None
    rent_source: Optional[str] = None
    extracted_via: list[str] = None
    warnings: list[str] = None

    def __post_init__(self):
        self.extracted_via = self.extracted_via or []
        self.warnings = self.warnings or []


# ---- detection -------------------------------------------------------------

def detect_source(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "redfin.com" in host:
        return "redfin"
    if "zillow.com" in host:
        return "zillow"
    if "realtor.com" in host:
        return "realtor"
    return "unknown"


# ---- shared fetchers -------------------------------------------------------

def _fetch(url: str, **kwargs) -> str:
    r = httpx.get(url, headers=HEADERS, follow_redirects=True, timeout=20, **kwargs)
    r.raise_for_status()
    return r.text


def _parse_zip_from_text(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{5})\b(?!-?\d)", text)
    return m.group(1) if m else None


# ---- Redfin ----------------------------------------------------------------

REDFIN_PROPERTY_ID_RE = re.compile(r"/home/(\d+)")
REDFIN_ZIP_IN_URL_RE  = re.compile(r"-(\d{5})/home/\d+")


def _ingest_redfin(url: str) -> IngestedProperty:
    out = IngestedProperty(source="redfin", url=url)
    pid_match = REDFIN_PROPERTY_ID_RE.search(url)
    if not pid_match:
        out.warnings.append("could not find property id in URL")
        return out
    property_id = pid_match.group(1)
    url_zip_match = REDFIN_ZIP_IN_URL_RE.search(url)
    expected_zip = url_zip_match.group(1) if url_zip_match else None

    # Strategy 1: Redfin's stingray initialInfo. Returns a JSON envelope
    # with the property block.
    path = urlparse(url).path
    stingray_404 = False
    try:
        body = _fetch(f"https://www.redfin.com/stingray/api/home/details/initialInfo?path={path}")
        data = json.loads(body.replace("{}&&", ""))
        info = data.get("payload") or {}
        addr = info.get("addressInfo") or {}
        out.address = addr.get("street")
        out.city = addr.get("city")
        out.state = addr.get("state")
        out.zip = addr.get("zip")
        out.listed_price = info.get("priceInfo", {}).get("amount")
        out.beds = info.get("beds")
        out.baths = info.get("baths")
        out.sqft = info.get("sqFt", {}).get("value") if isinstance(info.get("sqFt"), dict) else info.get("sqFt")
        out.year_built = info.get("yearBuilt")
        out.lot_size = info.get("lotSize")
        out.extracted_via.append("stingray:initialInfo")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            stingray_404 = True
            out.warnings.append("Redfin stingray API: 404 (listing not found / unlisted / region-locked).")
        else:
            out.warnings.append(f"stingray initialInfo failed: HTTP {e.response.status_code}")
    except Exception as e:
        out.warnings.append(f"stingray initialInfo failed: {type(e).__name__}")

    # If stingray returned 404, the listing doesn't exist on Redfin's API.
    # Don't fall through to HTML scraping — the search page would yield a
    # *different* listing's data and the user would silently underwrite
    # the wrong property. Fail loud instead.
    if stingray_404:
        return out

    # If stingray returned anything other than 200 (403 ‘Forbidden’,
    # rate-limited, or region-blocked), refuse to fall through to HTML
    # scraping for the same reason: scraping the redirect/search page
    # silently returns a different property. Better to fail loud than to
    # underwrite the wrong house.
    if not out.address and out.listed_price is None and not stingray_404:
        out.warnings.append(
            "Redfin API blocked (likely 403/rate limit). HTML fallback skipped to "
            "avoid mixing listings. Enter the property fields manually below."
        )
        # Still try ZORI for rent estimate when we have a URL zip.
        out.zip = expected_zip
        return out

    # Strategy 2: Redfin AVM endpoint for rent + value comp
    try:
        body = _fetch(
            f"https://www.redfin.com/stingray/api/home/details/avm?propertyId={property_id}&accessLevel=1"
        )
        data = json.loads(body.replace("{}&&", ""))
        avm = (data.get("payload") or {}).get("predictedValue") or {}
        rent = (data.get("payload") or {}).get("predictedRent")
        if isinstance(rent, dict):
            out.rent_estimate = rent.get("amount")
            out.rent_source = "redfin AVM"
        if not out.listed_price and avm:
            out.listed_price = avm.get("amount")
        out.extracted_via.append("stingray:avm")
    except Exception:
        pass

    # Strategy 3: HTML fallback for any field still missing. Only run when
    # the URL↔ZIP we expected matches what Redfin gave us — otherwise we
    # might enrich with the wrong listing's text.
    addresses_match = (
        expected_zip is None
        or out.zip is None
        or expected_zip == str(out.zip).strip()
    )
    if not addresses_match:
        out.warnings.append(
            f"URL zip {expected_zip} disagrees with Redfin response zip {out.zip} — "
            f"refusing to enrich from HTML to avoid mixing listings."
        )
        return out

    if not all([out.listed_price, out.sqft, out.beds]):
        try:
            html = _fetch(url)
            _enrich_from_html(out, html)
        except Exception as e:
            out.warnings.append(f"HTML fetch blocked: {type(e).__name__}")

    # Final URL↔response zip check after any HTML enrichment.
    if expected_zip and out.zip and expected_zip != str(out.zip).strip():
        out.warnings.append(
            f"Final zip mismatch: URL says {expected_zip}, extracted data says {out.zip}. "
            f"Blanking pricing/size fields — refusing to underwrite the wrong property."
        )
        out.address = out.city = out.state = None
        out.zip = expected_zip
        out.listed_price = out.beds = out.baths = out.sqft = None
        out.year_built = out.lot_size = None
        out.rent_estimate = None
        out.rent_source = None

    return out


# ---- Zillow / Realtor (HTML strategies) ------------------------------------

def _ingest_zillow(url: str) -> IngestedProperty:
    out = IngestedProperty(source="zillow", url=url)
    try:
        html = _fetch(url)
    except Exception as e:
        out.warnings.append(f"Zillow blocks scraping in this region: {type(e).__name__}")
        return out
    _enrich_from_html(out, html)
    return out


def _ingest_realtor(url: str) -> IngestedProperty:
    out = IngestedProperty(source="realtor", url=url)
    try:
        html = _fetch(url)
    except Exception as e:
        out.warnings.append(f"Realtor.com blocks scraping in this region: {type(e).__name__}")
        return out
    _enrich_from_html(out, html)
    return out


JSON_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
# Common visible-text patterns on listing pages.
PRICE_RE = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,8})(?:\.\d+)?")
BEDS_RE = re.compile(r"([0-9]+(?:\.[05])?)\s*(?:bd|bed|beds|bedrooms?)\b", re.I)
BATHS_RE = re.compile(r"([0-9]+(?:\.[05])?)\s*(?:ba|bath|baths|bathrooms?)\b", re.I)
SQFT_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,6})\s*(?:sq\s*ft|sqft|square\s+feet)", re.I)
YEAR_RE = re.compile(r"(?:built|year)\s*(?:in|built)?[:\s]*([12][0-9]{3})", re.I)
ADDRESS_RE = re.compile(r'"streetAddress"\s*:\s*"([^"]+)"')
CITY_RE = re.compile(r'"addressLocality"\s*:\s*"([^"]+)"')
STATE_RE = re.compile(r'"addressRegion"\s*:\s*"([A-Z]{2})"')
ZIP_RE = re.compile(r'"postalCode"\s*:\s*"(\d{5})"')


def _enrich_from_html(out: IngestedProperty, html: str) -> None:
    """Best-effort enrich any unfilled field from page HTML."""
    # JSON-LD first (most reliable when present)
    for m in JSON_LD_RE.finditer(html):
        try:
            blob = json.loads(m.group(1))
        except Exception:
            continue
        # JSON-LD may be an array
        for ld in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(ld, dict):
                continue
            addr = ld.get("address") or {}
            if isinstance(addr, dict):
                out.address = out.address or addr.get("streetAddress")
                out.city = out.city or addr.get("addressLocality")
                out.state = out.state or addr.get("addressRegion")
                out.zip = out.zip or addr.get("postalCode")
            offers = ld.get("offers") or {}
            if isinstance(offers, dict) and not out.listed_price:
                p = offers.get("price")
                if p is not None:
                    try: out.listed_price = float(p)
                    except Exception: pass
            if not out.beds:
                v = ld.get("numberOfRooms") or ld.get("numberOfBedrooms")
                if v is not None:
                    try: out.beds = float(v)
                    except Exception: pass
            fs = ld.get("floorSize") or {}
            if isinstance(fs, dict) and not out.sqft:
                v = fs.get("value")
                if v is not None:
                    try: out.sqft = float(v)
                    except Exception: pass
        if out.listed_price and out.address:
            out.extracted_via.append("json-ld")
            break

    # Regex on visible text
    if not out.address:
        m = ADDRESS_RE.search(html); out.address = m.group(1) if m else out.address
    if not out.city:
        m = CITY_RE.search(html); out.city = m.group(1) if m else out.city
    if not out.state:
        m = STATE_RE.search(html); out.state = m.group(1) if m else out.state
    if not out.zip:
        m = ZIP_RE.search(html); out.zip = m.group(1) if m else out.zip
    if not out.listed_price:
        m = PRICE_RE.search(html)
        if m:
            try:
                out.listed_price = float(m.group(1).replace(",", ""))
                out.extracted_via.append("regex:price")
            except Exception:
                pass
    if not out.beds:
        m = BEDS_RE.search(html)
        if m:
            try: out.beds = float(m.group(1)); out.extracted_via.append("regex:beds")
            except Exception: pass
    if not out.baths:
        m = BATHS_RE.search(html)
        if m:
            try: out.baths = float(m.group(1))
            except Exception: pass
    if not out.sqft:
        m = SQFT_RE.search(html)
        if m:
            try: out.sqft = float(m.group(1).replace(",", "")); out.extracted_via.append("regex:sqft")
            except Exception: pass
    if not out.year_built:
        m = YEAR_RE.search(html)
        if m:
            try: out.year_built = int(m.group(1))
            except Exception: pass


# ---- public entry point ----------------------------------------------------

def ingest(url: str) -> IngestedProperty:
    """Dispatch to the right scraper based on URL domain."""
    if not url or not url.startswith(("http://", "https://")):
        return IngestedProperty(source="unknown", url=url, warnings=["invalid URL"])
    src = detect_source(url)
    if src == "redfin":
        return _ingest_redfin(url)
    if src == "zillow":
        return _ingest_zillow(url)
    if src == "realtor":
        return _ingest_realtor(url)
    return IngestedProperty(source="unknown", url=url,
                            warnings=[f"unsupported source; expected redfin / zillow / realtor"])


def rent_estimate_from_zori(con, zip_code: str) -> Optional[float]:
    """Fallback: latest ZORI for this zip. Use when the listing doesn't carry
    its own rent estimate. Returns None if zip not in our index."""
    if not zip_code:
        return None
    try:
        row = con.execute(
            """SELECT value FROM zillow_zori
               WHERE zip = ?
               ORDER BY period DESC LIMIT 1""",
            [str(zip_code).zfill(5)],
        ).fetchone()
    except Exception:
        return None
    return float(row[0]) if row else None


def to_dict(p: IngestedProperty) -> dict:
    return asdict(p)
