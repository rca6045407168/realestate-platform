"""Redfin Data Center weekly market metrics.

Public TSVs (gzipped) per geography. Columns include median_sale_price,
median_list_price, median_dom, sale_to_list, inventory, etc. Roughly 5 years
of weekly history.
"""
from __future__ import annotations
import gzip
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

URLS = {
    "zip": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/zip_code_market_tracker.tsv000.gz",
    "county": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/county_market_tracker.tsv000.gz",
    "metro": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/redfin_metro_market_tracker.tsv000.gz",
}

# Map Redfin TSV column names → our schema column names.
COL_MAP = {
    "median_sale_price": "median_sale_price",
    "median_list_price": "median_list_price",
    "homes_sold": "homes_sold",
    "new_listings": "new_listings",
    "inventory": "inventory",
    "median_dom": "median_days_on_market",
    "avg_sale_to_list": "sale_to_list",
    "sold_above_list": "pct_homes_sold_above_list",
    "off_market_in_two_weeks": "off_market_in_two_weeks",
}


def _normalize_chunk(df: pd.DataFrame, geo_type: str, cutoff) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df["period"] = pd.to_datetime(df["period_begin"], errors="coerce").dt.date
    df = df[df["period"] >= cutoff]
    if df.empty:
        return df
    if geo_type == "zip":
        df["geo_id"] = (
            df["region"].astype(str).str.replace("Zip Code: ", "", regex=False).str.zfill(5)
        )
    else:
        df["geo_id"] = df["region"].astype(str)
    df["geo_type"] = geo_type
    for src, dst in COL_MAP.items():
        df[dst] = pd.to_numeric(df.get(src), errors="coerce")
    out_cols = ["geo_id", "geo_type", "period"] + list(COL_MAP.values())
    return df[out_cols].drop_duplicates(["geo_id", "geo_type", "period"])


def load(con: duckdb.DuckDBPyConnection, geos=("zip", "county"), refresh: bool = False, months: int = 24) -> int:
    """Stream-load. Files are 1GB+ compressed; we read in chunks and keep
    only the most recent `months` months to fit in memory."""
    cutoff = (pd.Timestamp.today() - pd.DateOffset(months=months)).date()
    total = 0
    usecols = ["PERIOD_BEGIN", "REGION", "STATE_CODE", "PROPERTY_TYPE"] + [c.upper() for c in COL_MAP.keys()]
    for geo in geos:
        path = download(URLS[geo], suffix=".tsv.gz", refresh=refresh)
        with gzip.open(path, "rt") as f:
            for chunk in pd.read_csv(f, sep="\t", chunksize=200_000, usecols=lambda c: c.upper() in usecols, low_memory=False):
                # All Redfin data has property_type "All Residential" rolled up; keep only that bucket
                lc = {c.lower(): c for c in chunk.columns}
                if "property_type" in lc:
                    chunk = chunk[chunk[lc["property_type"]] == "All Residential"]
                norm = _normalize_chunk(chunk, geo, cutoff)
                if not norm.empty:
                    total += upsert_df(con, "redfin_market", norm)
    return total
