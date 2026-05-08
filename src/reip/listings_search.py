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


# Redfin uses CBSA codes as region IDs for most metros, with region_type=6
# meaning “metro / market.” A handful of major metros (Atlanta, Kansas City,
# Denver, etc.) use legacy IDs that pre-date the OMB delineation; for those
# we record the override below. region_id+region_type was probed against
# the live Redfin /stingray/api/gis search for each entry to make sure it
# returns active listings. Probed 2026-05-08.
#
# Grouped by archetype per the framework's §4 (Coastal Gateway / Sun Belt /
# Cashflow Heartland / Boom-Bust / Resource & Niche). Mixed metros default
# to whichever bucket dominates.
MARKETS: dict[str, dict] = {
    # ---- Coastal Gateway ------------------------------------------------
    "35620": {"name": "New York, NY-NJ-PA",        "region_id": 35620, "region_type": 6, "market": "new-york",       "archetype_hint": "Coastal Gateway"},
    "31080": {"name": "Los Angeles-Long Beach, CA","region_id": 31080, "region_type": 6, "market": "los-angeles",    "archetype_hint": "Coastal Gateway"},
    "41860": {"name": "San Francisco-Oakland, CA", "region_id": 41860, "region_type": 6, "market": "san-francisco",  "archetype_hint": "Coastal Gateway"},
    "42660": {"name": "Seattle-Tacoma, WA",        "region_id": 42660, "region_type": 6, "market": "seattle",        "archetype_hint": "Coastal Gateway"},
    "14460": {"name": "Boston-Cambridge, MA-NH",   "region_id": 14460, "region_type": 6, "market": "boston",         "archetype_hint": "Coastal Gateway"},
    "47900": {"name": "Washington, DC-VA-MD-WV",   "region_id": 47900, "region_type": 6, "market": "washington-dc",  "archetype_hint": "Coastal Gateway"},
    "41940": {"name": "San Jose-Sunnyvale, CA",    "region_id": 41940, "region_type": 6, "market": "san-jose",       "archetype_hint": "Coastal Gateway"},
    "41740": {"name": "San Diego-Carlsbad, CA",    "region_id": 41740, "region_type": 6, "market": "san-diego",      "archetype_hint": "Coastal Gateway"},
    "37980": {"name": "Philadelphia-Camden, PA-NJ","region_id": 37980, "region_type": 6, "market": "philadelphia",   "archetype_hint": "Coastal Gateway"},

    # ---- Sun Belt Growth ------------------------------------------------
    "19100": {"name": "Dallas-Fort Worth, TX",     "region_id": 19100, "region_type": 6, "market": "dallas",         "archetype_hint": "Sun Belt Growth"},
    "26420": {"name": "Houston-Pasadena, TX",      "region_id": 26420, "region_type": 6, "market": "houston",        "archetype_hint": "Sun Belt Growth"},
    "12420": {"name": "Austin-Round Rock, TX",     "region_id": 12420, "region_type": 6, "market": "austin",         "archetype_hint": "Sun Belt Growth"},
    "36740": {"name": "Orlando-Kissimmee, FL",     "region_id": 36740, "region_type": 6, "market": "orlando",        "archetype_hint": "Sun Belt Growth"},
    "45300": {"name": "Tampa-St. Petersburg, FL",  "region_id": 45300, "region_type": 6, "market": "tampa",          "archetype_hint": "Sun Belt Growth"},
    "33100": {"name": "Miami-Fort Lauderdale, FL", "region_id": 33100, "region_type": 6, "market": "miami",          "archetype_hint": "Sun Belt Growth"},
    "34980": {"name": "Nashville-Davidson, TN",    "region_id": 34980, "region_type": 6, "market": "nashville",      "archetype_hint": "Sun Belt Growth"},
    "39580": {"name": "Raleigh-Cary, NC",          "region_id": 39580, "region_type": 6, "market": "raleigh",        "archetype_hint": "Sun Belt Growth"},
    "16740": {"name": "Charlotte-Concord, NC-SC",  "region_id": 16740, "region_type": 6, "market": "charlotte",      "archetype_hint": "Sun Belt Growth"},
    "38060": {"name": "Phoenix-Mesa-Chandler, AZ", "region_id": 38060, "region_type": 6, "market": "phoenix",        "archetype_hint": "Sun Belt Growth"},
    "27260": {"name": "Jacksonville, FL",          "region_id": 27260, "region_type": 6, "market": "jacksonville",   "archetype_hint": "Sun Belt Growth"},
    "41700": {"name": "San Antonio-New Braunfels, TX","region_id": 41700, "region_type": 6, "market": "san-antonio",  "archetype_hint": "Sun Belt Growth"},
    "41620": {"name": "Salt Lake City, UT",        "region_id": 41620, "region_type": 6, "market": "salt-lake-city", "archetype_hint": "Sun Belt Growth"},
    "12060": {"name": "Atlanta-Sandy Springs, GA", "region_id": 1407,  "region_type": 2, "market": "atlanta",        "archetype_hint": "Sun Belt Growth"},  # CBSA returns 0; fall back to city id 1407

    # ---- Cashflow Heartland ---------------------------------------------
    "32820": {"name": "Memphis, TN-MS-AR",         "region_id": 12260, "region_type": 6, "market": "memphis",        "archetype_hint": "Cashflow Heartland"},
    "26900": {"name": "Indianapolis-Carmel, IN",   "region_id": 11770, "region_type": 6, "market": "indianapolis",   "archetype_hint": "Cashflow Heartland"},
    "28140": {"name": "Kansas City, MO-KS",        "region_id": 9668,  "region_type": 6, "market": "kansas-city",    "archetype_hint": "Cashflow Heartland"},
    "13820": {"name": "Birmingham-Hoover, AL",     "region_id": 1788,  "region_type": 6, "market": "birmingham",     "archetype_hint": "Cashflow Heartland"},
    "17460": {"name": "Cleveland-Elyria, OH",      "region_id": 5022,  "region_type": 6, "market": "cleveland",      "archetype_hint": "Cashflow Heartland"},
    "38300": {"name": "Pittsburgh, PA",            "region_id": 29470, "region_type": 6, "market": "pittsburgh",     "archetype_hint": "Cashflow Heartland"},
    "19820": {"name": "Detroit-Warren-Dearborn, MI","region_id": 19820, "region_type": 6, "market": "detroit",        "archetype_hint": "Cashflow Heartland"},
    "17140": {"name": "Cincinnati, OH-KY-IN",      "region_id": 17140, "region_type": 6, "market": "cincinnati",     "archetype_hint": "Cashflow Heartland"},
    "41180": {"name": "St. Louis, MO-IL",          "region_id": 41180, "region_type": 6, "market": "st-louis",       "archetype_hint": "Cashflow Heartland"},
    "36420": {"name": "Oklahoma City, OK",         "region_id": 36420, "region_type": 6, "market": "oklahoma-city",  "archetype_hint": "Cashflow Heartland"},
    "33340": {"name": "Milwaukee-Waukesha, WI",    "region_id": 33340, "region_type": 6, "market": "milwaukee",      "archetype_hint": "Cashflow Heartland"},
    "16980": {"name": "Chicago-Naperville, IL-IN", "region_id": 16980, "region_type": 6, "market": "chicago",        "archetype_hint": "Cashflow Heartland"},

    # ---- Boom-Bust Beta -------------------------------------------------
    "29820": {"name": "Las Vegas-Henderson, NV",   "region_id": 29820, "region_type": 6, "market": "las-vegas",      "archetype_hint": "Boom-Bust Beta"},
    "40140": {"name": "Riverside-San Bernardino, CA","region_id": 40140, "region_type": 6, "market": "riverside",     "archetype_hint": "Boom-Bust Beta"},
    "39900": {"name": "Reno, NV",                  "region_id": 39900, "region_type": 6, "market": "reno",           "archetype_hint": "Boom-Bust Beta"},

    # ---- Resource & Niche -----------------------------------------------
    "19740": {"name": "Denver-Aurora, CO",         "region_id": 19740, "region_type": 6, "market": "denver",         "archetype_hint": "Mixed"},
    "38900": {"name": "Portland-Vancouver, OR-WA", "region_id": 38900, "region_type": 6, "market": "portland",       "archetype_hint": "Mixed"},
    "40900": {"name": "Sacramento-Roseville, CA",  "region_id": 40900, "region_type": 6, "market": "sacramento",     "archetype_hint": "Mixed"},
    "33460": {"name": "Minneapolis-St. Paul, MN-WI","region_id": 33460, "region_type": 6, "market": "minneapolis",    "archetype_hint": "Mixed"},
    "14260": {"name": "Boise City, ID",            "region_id": 14260, "region_type": 6, "market": "boise",          "archetype_hint": "Resource & Niche"},
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
