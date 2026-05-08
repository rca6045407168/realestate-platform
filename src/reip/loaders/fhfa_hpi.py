"""FHFA House Price Index — MSA quarterly.

Public CSV. Columns: place_name, place_id (CBSA), yr, qtr, index_nsa, index_sa.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

URL = "https://www.fhfa.gov/hpi/download/quarterly_datasets/hpi_at_metro.csv"


def load(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    path = download(URL, suffix=".csv", refresh=refresh)
    # FHFA metro file is headerless. Documented columns:
    #   metro_name, cbsa_code, year, quarter, index_nsa, index_sa
    df = pd.read_csv(
        path,
        names=["metro_name", "cbsa_code", "year", "quarter", "index_nsa", "index_sa"],
        header=None, low_memory=False,
    )
    df["index_nsa"] = pd.to_numeric(df["index_nsa"], errors="coerce")
    df = df.dropna(subset=["index_nsa"])
    df = df[df["year"].astype(str).str.isnumeric() & df["quarter"].astype(str).str.isnumeric()]
    df["period"] = pd.to_datetime(
        df["year"].astype(int).astype(str) + "-" + (df["quarter"].astype(int) * 3).astype(str).str.zfill(2) + "-01",
        errors="coerce",
    ).dt.date
    out = pd.DataFrame({
        "cbsa_code": df["cbsa_code"].astype(str),
        "period": df["period"],
        "hpi": df["index_nsa"],
    }).dropna()
    return upsert_df(con, "fhfa_hpi_metro", out)
