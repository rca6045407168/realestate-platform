"""Zillow Research data: ZHVI (home value index) + ZORI (rent index) by zip.

Public CSVs, no auth. Files are wide-format (one column per month). We melt
into long form and write to duckdb.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

ZHVI_ZIP_URL = (
    "https://files.zillowstatic.com/research/public_csvs/zhvi/"
    "Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
)
ZORI_ZIP_URL = (
    "https://files.zillowstatic.com/research/public_csvs/zori/"
    "Zip_zori_uc_sfrcondomfr_sm_month.csv"
)


def _melt(path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"RegionName": str})
    id_cols = [c for c in df.columns if not c[:4].isdigit()]
    val_cols = [c for c in df.columns if c[:4].isdigit() and "-" in c]
    long = df.melt(id_vars=id_cols, value_vars=val_cols, var_name="period", value_name="value")
    long = long.dropna(subset=["value"])
    long["zip"] = long["RegionName"].str.zfill(5)
    long["period"] = pd.to_datetime(long["period"]).dt.date
    return long[["zip", "period", "value"]]


def load_zhvi(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    path = download(ZHVI_ZIP_URL, suffix=".csv", refresh=refresh)
    df = _melt(path)
    return upsert_df(con, "zillow_zhvi", df)


def load_zori(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    path = download(ZORI_ZIP_URL, suffix=".csv", refresh=refresh)
    df = _melt(path)
    return upsert_df(con, "zillow_zori", df)


def load(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    return load_zhvi(con, refresh) + load_zori(con, refresh)
