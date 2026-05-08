"""HUD Fair Market Rents — Section 8 rent ceiling, by zip.

Annual. The HUD User API serves zip-level FMRs for Small Area FMR metros.
For non-SAFMR areas we fall back to county-level (later joined via the zip
crosswalk).

Requires HUD_API_TOKEN. Free, instant signup at
https://www.huduser.gov/hudapi/public/register
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import get_json
from ..store import upsert_df
from ..config import HUD_API_TOKEN

BASE = "https://www.huduser.gov/hudapi/public/fmr"
STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME",
    "MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA",
    "RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
]


def load(con: duckdb.DuckDBPyConnection, year: int = 2025, states: list[str] | None = None) -> int:
    if not HUD_API_TOKEN:
        print("HUD_API_TOKEN not set — skipping FMR. Get a free token at https://www.huduser.gov/hudapi/public/register")
        return 0
    headers = {"Authorization": f"Bearer {HUD_API_TOKEN}"}
    states = states or STATES
    rows = []
    for st in states:
        url = f"{BASE}/data/{st}?year={year}"
        try:
            data = get_json(url, headers=headers)
        except Exception as e:
            print(f"HUD FMR {st}: {e}")
            continue
        # Response varies — we look for zip-level entries with bedrooms.
        for area in data.get("data", {}).get("smallareafmrs", []) or []:
            zp = str(area.get("zip_code") or "").zfill(5)
            if not zp.isdigit():
                continue
            rows.append({
                "zip": zp, "year": year,
                "fmr_0br": area.get("Efficiency"),
                "fmr_1br": area.get("One-Bedroom"),
                "fmr_2br": area.get("Two-Bedroom"),
                "fmr_3br": area.get("Three-Bedroom"),
                "fmr_4br": area.get("Four-Bedroom"),
            })
    if not rows:
        return 0
    return upsert_df(con, "hud_fmr", pd.DataFrame(rows))
