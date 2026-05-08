"""Live listings via Redfin's stingray/api/gis search endpoint.

The property-detail endpoint (`/details/initialInfo`) is gated, but the
search endpoint (`/api/gis`) returns full property cards including price,
beds, baths, sqft, year_built, lot, hoa, dom, mls#, and a permalink.
We use that as the canonical source for the screener.

Markets are seeded with the spec's five launch MSAs (Memphis,
Indianapolis, Kansas City, Birmingham, Cleveland) plus a few popular
comparison markets. The CBSA → Redfin region_id mapping is hand-coded;
it's stable enough across years that this is the right tradeoff vs.
building a region-search resolver.

The response envelope is `{}&&{...}` Backbone-style; we strip the prefix.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional
import json
import httpx


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json,text/javascript,*/*;q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
}

BASE = "https://www.redfin.com/stingray/api/gis"


# Each entry was probed live against /stingray/api/gis on 2026-05-08 and
# only included if listings come back with the correct city. Redfin's
# region_id is *internal* (not the CBSA code), region_type=6 ≈ metro/MSA
# in most cases, region_type=5 ≈ county. Adding new metros means probing
# for the right (region_id, region_type) pair manually — their HTML pages
# are CloudFront-gated so dynamic resolution isn't possible.
#
# Earlier expansion attempt naively used CBSA codes as region_ids, which
# silently mapped most metros to wrong cities (Seattle defaults). That's
# why this list is curated, not generated.
#
# CBSA code is preserved as the dict key so school + ACS overlays still
# join correctly.
MARKETS: dict[str, dict] = {
    # ---- Coastal Gateway ------------------------------------------------
    "35620": {"name": "New York, NY-NJ-PA",         "region_id": 30749, "region_type": 6, "market": "new-york",      "archetype_hint": "Coastal Gateway"},
    "31080": {"name": "Los Angeles-Long Beach, CA", "region_id": 11203, "region_type": 6, "market": "los-angeles",   "archetype_hint": "Coastal Gateway"},
    "41860": {"name": "San Francisco-Oakland, CA",  "region_id": 17151, "region_type": 6, "market": "san-francisco", "archetype_hint": "Coastal Gateway"},
    "42660": {"name": "Seattle-Tacoma, WA",         "region_id": 16163, "region_type": 5, "market": "seattle",       "archetype_hint": "Coastal Gateway"},
    "47900": {"name": "Washington, DC-VA-MD-WV",    "region_id": 12839, "region_type": 6, "market": "washington-dc", "archetype_hint": "Coastal Gateway"},
    "41940": {"name": "San Jose-Sunnyvale, CA",     "region_id": 17420, "region_type": 6, "market": "san-jose",      "archetype_hint": "Coastal Gateway"},

    # ---- Sun Belt Growth ------------------------------------------------
    "26420": {"name": "Houston-Pasadena, TX",       "region_id": 8903,  "region_type": 6, "market": "houston",       "archetype_hint": "Sun Belt Growth"},
    "12420": {"name": "Austin-Round Rock, TX",      "region_id": 30818, "region_type": 6, "market": "austin",        "archetype_hint": "Sun Belt Growth"},
    "33100": {"name": "Miami-Fort Lauderdale, FL",  "region_id": 11458, "region_type": 6, "market": "miami",         "archetype_hint": "Sun Belt Growth"},
    "38060": {"name": "Phoenix-Mesa-Chandler, AZ",  "region_id": 14240, "region_type": 6, "market": "phoenix",       "archetype_hint": "Sun Belt Growth"},

    # ---- Cashflow Heartland ---------------------------------------------
    "32820": {"name": "Memphis, TN-MS-AR",          "region_id": 12260, "region_type": 6, "market": "memphis",       "archetype_hint": "Cashflow Heartland"},
}

# Markets verified to NOT work with the IDs we tried — these need a
# different (region_id, region_type) combo. Listed here as a TODO so we
# don't expose them in the dropdown until verified. Add candidates by
# scraping a public Redfin URL like redfin.com/<state>/<city> and looking
# for region_id in the page (CloudFront blocks this from datacenters but
# works from a residential IP).
_TODO_MARKETS = [
    "Boston, MA", "Atlanta, GA", "Dallas-Fort Worth, TX",
    "Chicago, IL", "Philadelphia, PA", "Indianapolis, IN",
    "Charlotte, NC", "Nashville, TN", "Raleigh, NC",
    "Denver, CO", "Portland, OR", "Sacramento, CA",
    "San Diego, CA", "San Antonio, TX", "Detroit, MI",
    "Cincinnati, OH", "Cleveland, OH", "Pittsburgh, PA",
    "Birmingham, AL", "Kansas City, MO", "Las Vegas, NV",
    "Salt Lake City, UT", "Minneapolis, MN", "Tampa, FL",
    "Orlando, FL", "Jacksonville, FL", "St. Louis, MO",
    "Oklahoma City, OK", "Milwaukee, WI", "Boise, ID",
    "Reno, NV", "Riverside, CA",
]


@dataclass
class Listing:
    mls: str
    url: str
    address: Optional[str]
    city: Optional[str]
    state: Optional[str]
    zip: Optional[str]
    listed_price: Optional[float]
    beds: Optional[float]
    baths: Optional[float]
    sqft: Optional[float]
    year_built: Optional[int]
    lot_size: Optional[float]
    hoa_monthly: Optional[float]
    days_on_market: Optional[int]
    cbsa_code: str
    cbsa_name: str


def _gv(d, *path, default=None):
    cur = d
    for p in path:
        if cur is None or not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _parse_home(h: dict, cbsa_code: str, cbsa_name: str) -> Optional[Listing]:
    try:
        mls_v = _gv(h, "mlsId", "value")
        if mls_v is None:
            mls_v = h.get("mlsId") or str(h.get("propertyId") or "")
        time_ms = _gv(h, "timeOnRedfin", "value")
        dom = round(time_ms / (1000 * 60 * 60 * 24)) if time_ms else None
        return Listing(
            mls=str(mls_v),
            url="https://www.redfin.com" + (h.get("url") or ""),
            address=_gv(h, "streetLine", "value"),
            city=h.get("city"),
            state=h.get("state"),
            zip=str(h.get("zip") or "").zfill(5) or None,
            listed_price=_gv(h, "price", "value"),
            beds=h.get("beds"),
            baths=h.get("baths"),
            sqft=_gv(h, "sqFt", "value"),
            year_built=_gv(h, "yearBuilt", "value"),
            lot_size=_gv(h, "lotSize", "value"),
            hoa_monthly=_gv(h, "hoa", "value"),
            days_on_market=dom,
            cbsa_code=cbsa_code,
            cbsa_name=cbsa_name,
        )
    except Exception:
        return None


def search(cbsa_code: str, num_homes: int = 50,
           min_price: Optional[int] = None, max_price: Optional[int] = None,
           uipt: str = "1,2,3", status: str = "9",
           timeout: float = 20.0) -> tuple[list[Listing], list[str]]:
    """Pull active Redfin listings for one CBSA.

    Returns (listings, warnings). uipt="1,2,3" = SFR + condo + townhouse.
    status="9" = active for-sale.
    """
    market = MARKETS.get(cbsa_code)
    warnings: list[str] = []
    if not market:
        return [], [f"CBSA {cbsa_code} is not in the launch-markets allowlist. "
                    f"Add it to listings_search.MARKETS."]
    params = {
        "al": 1,
        "market": market["market"],
        "num_homes": num_homes,
        "ord": "days-on-redfin-asc",
        "page_number": 1,
        "region_id": market["region_id"],
        "region_type": market["region_type"],
        "sf": "1,2,3,5,6,7",
        "start": 0,
        "status": status,
        "uipt": uipt,
        "v": 8,
    }
    if min_price is not None: params["min_price"] = min_price
    if max_price is not None: params["max_price"] = max_price
    try:
        r = httpx.get(BASE, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        return [], [f"Redfin search failed: {type(e).__name__} ({e})."]
    body = r.text
    if body.startswith("{}&&"):
        body = body[4:]
    try:
        data = json.loads(body)
    except Exception as e:
        return [], [f"Redfin response was not JSON: {e}"]
    homes = (data.get("payload") or {}).get("homes") or []
    listings = [l for l in (_parse_home(h, cbsa_code, market["name"]) for h in homes) if l is not None]
    return listings, warnings


def list_markets() -> list[dict]:
    return [{"cbsa_code": k, **{kk: vv for kk, vv in v.items() if kk != "region_id"}}
            for k, v in MARKETS.items()]
