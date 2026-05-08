"""Zip → county FIPS crosswalk.

Source: HUD USPS crosswalk (free, no auth via huduser.gov bulk download).
Without HUD token we fall back to the smaller Census ZCTA-to-county
relationship file.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

# Census ZCTA-county relationship file (2020). Public, no auth.
CENSUS_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"


def load(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    path = download(CENSUS_URL, suffix=".txt", refresh=refresh)
    df = pd.read_csv(path, sep="|", dtype=str, low_memory=False)
    # Columns: GEOID_ZCTA5_20, GEOID_COUNTY_20, etc.
    df.columns = [c.lower() for c in df.columns]
    zip_col = next((c for c in df.columns if c.startswith("geoid_zcta")), None)
    cty_col = next((c for c in df.columns if c.startswith("geoid_county")), None)
    if zip_col is None or cty_col is None:
        raise RuntimeError(f"Could not find GEOID columns in {df.columns.tolist()}")
    df = df[df[zip_col].notna() & df[cty_col].notna()]
    out = pd.DataFrame({
        "zip": df[zip_col].str.zfill(5),
        "fips_county": df[cty_col].str.zfill(5),
        "weight": 1.0,
    })
    out = out[out["zip"].str.len() == 5]
    out = out.drop_duplicates(["zip", "fips_county"])
    return upsert_df(con, "zip_county_xwalk", out)
