"""Census ACS 5-year county estimates for population, income, households,
median home value, median gross rent.

Uses the public Census Data API. CENSUS_API_KEY recommended (free) but
endpoints work for low-volume calls without one.

Variables (ACS5 detailed tables):
  B01003_001E  Total population
  B11001_001E  Households
  B19013_001E  Median household income
  B25077_001E  Median home value
  B25064_001E  Median gross rent
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import get_json
from ..store import upsert_df
from ..config import CENSUS_API_KEY

VARS = {
    "B01003_001E": "population",
    "B11001_001E": "households",
    "B19013_001E": "median_household_income",
    "B25077_001E": "median_home_value",
    "B25064_001E": "median_gross_rent",
}


def _fetch(year: int, var_codes: list[str]) -> pd.DataFrame:
    base = f"https://api.census.gov/data/{year}/acs/acs5"
    params = {"get": ",".join(["NAME"] + var_codes), "for": "county:*"}
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    rows = get_json(f"{base}?{qs}")
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df["fips_county"] = df["state"].str.zfill(2) + df["county"].str.zfill(3)
    for v in var_codes:
        df[v] = pd.to_numeric(df[v], errors="coerce")
    df = df.rename(columns=VARS)
    df["year"] = year
    return df[["fips_county", "year"] + list(VARS.values())]


def load(con: duckdb.DuckDBPyConnection, years: list[int] | None = None) -> int:
    """Pull both endpoints needed for 5-yr CAGR. ACS lags ~1 year; we use
    2018 (5-yr ending 2018) and 2023 (5-yr ending 2023) for a 5-yr delta."""
    years = years or [2018, 2023]
    total = 0
    for y in years:
        try:
            df = _fetch(y, list(VARS.keys()))
            total += upsert_df(con, "acs_county", df)
        except Exception as e:
            print(f"ACS {y}: {e}")
    return total
