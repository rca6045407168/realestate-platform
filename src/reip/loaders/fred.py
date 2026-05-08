"""FRED macro series: 30-year mortgage rate, 10-year Treasury, CPI shelter.

Free API key required. https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import get_json
from ..store import upsert_df
from ..config import FRED_API_KEY

SERIES = [
    "MORTGAGE30US",   # 30-yr fixed mortgage rate
    "DGS10",          # 10-year Treasury yield
    "CPIHOSSL",       # CPI shelter
    "CSUSHPISA",      # Case-Shiller national HPI
    "MEHOINUSA672N",  # Real median household income
]


def load(con: duckdb.DuckDBPyConnection, series: list[str] | None = None) -> int:
    if not FRED_API_KEY:
        print("FRED_API_KEY not set — skipping macro. Free key at https://fred.stlouisfed.org/docs/api/api_key.html")
        return 0
    series = series or SERIES
    total = 0
    for sid in series:
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={sid}&api_key={FRED_API_KEY}&file_type=json"
        )
        try:
            data = get_json(url)
        except Exception as e:
            print(f"FRED {sid}: {e}")
            continue
        obs = data.get("observations", [])
        if not obs:
            continue
        df = pd.DataFrame(obs)
        df["series_id"] = sid
        df["period"] = pd.to_datetime(df["date"]).dt.date
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        total += upsert_df(con, "fred_macro", df[["series_id", "period", "value"]])
    return total
