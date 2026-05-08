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


# Hand-coded launch markets. Each entry maps a CBSA code (per
# county_cbsa_xwalk) to the Redfin region_id + region_type pair that
# returns the bulk of the metro. region_type=6 = ZIP, type=2 = city,
# type=5 = county. We use county/metro IDs where Redfin exposes them.
MARKETS: dict[str, dict] = {
    "32820": {  # Memphis, TN-MS-AR
        "name":  "Memphis",
        "region_id":   12260, "region_type": 6,  # Memphis market id
        "market":      "memphis",
    },
    "26900": {  # Indianapolis-Carmel-Anderson, IN
        "name":  "Indianapolis",
        "region_id":   11770, "region_type": 6,
        "market":      "indianapolis",
    },
    "28140": {  # Kansas City, MO-KS
        "name":  "Kansas City",
        "region_id":   9668,  "region_type": 6,
        "market":      "kansas-city",
    },
    "13820": {  # Birmingham-Hoover, AL
        "name":  "Birmingham",
        "region_id":   1788,  "region_type": 6,
        "market":      "birmingham",
    },
    "17460": {  # Cleveland-Elyria, OH
        "name":  "Cleveland",
        "region_id":   5022,  "region_type": 6,
        "market":      "cleveland",
    },
    "38300": {  # Pittsburgh, PA
        "name":  "Pittsburgh",
        "region_id":   29470, "region_type": 6,
        "market":      "pittsburgh",
    },
}


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
