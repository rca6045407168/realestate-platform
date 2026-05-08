"""Redfin live listings via the unofficial stingray/api/gis endpoint.

Ported from project_pikachu (2019). Each market in markets.py supplies
cookies + headers + params for a region search. We pull the active listing
list and stamp our standard mortgage-carry calculation.
"""
from __future__ import annotations
import json
import os
import httpx
import pandas as pd
import duckdb
from ..store import upsert_df

# Sensible defaults — override in CLI / env for live runs.
DEFAULT_INTEREST_RATE = 0.07
DEFAULT_LTV = 0.80
DEFAULT_MORTGAGE_TERM_YEARS = 30
DEFAULT_INSURANCE_COST = 1500


def _carry(price: float, hoa: float, rate: float, ltv: float, term_yrs: int, insurance: float) -> float:
    if not price or pd.isna(price):
        return float("nan")
    monthly_rate = rate / 12
    n = term_yrs * 12
    loan = price * ltv
    if monthly_rate == 0:
        principal_payment = loan / n
    else:
        principal_payment = loan * (monthly_rate * (1 + monthly_rate) ** n) / ((1 + monthly_rate) ** n - 1)
    return round(principal_payment + (hoa or 0) + insurance / 12, 2)


def fetch_listings(region: str, cookies: dict, headers: dict, params: dict) -> list[dict]:
    """Hit Redfin stingray/api/gis. Returns the homes payload."""
    r = httpx.get(
        "https://www.redfin.com/stingray/api/gis",
        cookies=cookies, headers=headers, params=params, timeout=30,
    )
    r.raise_for_status()
    body = r.text.replace("{}&&", "")
    return json.loads(body).get("payload", {}).get("homes", [])


def normalize(houses: list[dict], region: str, rate: float, ltv: float, term_yrs: int, insurance: float) -> pd.DataFrame:
    out = []
    for h in houses:
        try:
            mls = str(h.get("mlsId", {}).get("value")) if isinstance(h.get("mlsId"), dict) else str(h.get("mlsId"))
            price = h.get("price", {}).get("value")
            hoa = (h.get("hoa") or {}).get("value") or 0
            sqft = (h.get("sqFt") or {}).get("value")
            beds = h.get("beds")
            baths = h.get("baths")
            year_built = (h.get("yearBuilt") or {}).get("value")
            lot_size = (h.get("lotSize") or {}).get("value")
            time_on_redfin_ms = (h.get("timeOnRedfin") or {}).get("value")
            dom = round(time_on_redfin_ms / (1000 * 60 * 60 * 24)) if time_on_redfin_ms else None
            row = {
                "mls": mls,
                "region": region,
                "url": "https://www.redfin.com" + (h.get("url") or ""),
                "street_address": (h.get("streetLine") or {}).get("value"),
                "city": h.get("city"),
                "state": h.get("state"),
                "zip": str(h.get("zip") or "").zfill(5),
                "listed_price": price,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "lot_size": lot_size,
                "year_built": year_built,
                "hoa": hoa,
                "days_on_market": dom,
                "monthly_expense": _carry(price, hoa, rate, ltv, term_yrs, insurance),
            }
            out.append(row)
        except Exception:
            continue
    return pd.DataFrame(out)


def load(
    con: duckdb.DuckDBPyConnection,
    region_specs: list[dict],
    rate: float = DEFAULT_INTEREST_RATE,
    ltv: float = DEFAULT_LTV,
    term_yrs: int = DEFAULT_MORTGAGE_TERM_YEARS,
    insurance: float = DEFAULT_INSURANCE_COST,
) -> int:
    """region_specs is a list of dicts with keys: region, cookies, headers, params."""
    total = 0
    for spec in region_specs:
        try:
            houses = fetch_listings(spec["region"], spec["cookies"], spec["headers"], spec["params"])
        except Exception as e:
            print(f"Redfin listings {spec.get('region')}: {e}")
            continue
        df = normalize(houses, spec["region"], rate, ltv, term_yrs, insurance)
        if not df.empty:
            total += upsert_df(con, "redfin_listings", df)
    return total
