"""Census Building Permits Survey (BPS) — county-level monthly.

Leading indicator of new housing supply. Files are at:
  https://www2.census.gov/econ/bps/County/co<YY><M>c.txt
where YY is 2-digit year and M is 'a','y' (annual cum).

We pull recent monthly cumulative county files. The format is fixed-width
with header rows; we use Census's standard column names.
"""
from __future__ import annotations
import io
from datetime import date
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

BASE = "https://www2.census.gov/econ/bps/County"


def _build_url(year: int, month: int) -> str:
    yy = str(year)[-2:]
    # Monthly cumulative: co<yy><MM>c.txt; year cumulative: co<yy>a.txt
    return f"{BASE}/co{yy}{month:02d}c.txt"


def _parse(path) -> pd.DataFrame | None:
    try:
        # Files are CSV-ish despite .txt extension. Skip first 2 header rows;
        # Census documents columns starting at row 3.
        df = pd.read_csv(path, skiprows=2, header=None, low_memory=False)
    except Exception:
        return None
    # Standard county BPS layout has these columns (post-2014):
    # 0 Survey Date, 1 FIPS State, 2 FIPS County, 3 Region, 4 Division,
    # 5 County Name, 6 1u Bldgs, 7 1u Units, 8 1u Value,
    # 9 2u Bldgs, 10 2u Units, 11 2u Value,
    # 12 3-4u Bldgs, 13 3-4u Units, 14 3-4u Value,
    # 15 5+u Bldgs, 16 5+u Units, 17 5+u Value
    if df.shape[1] < 18:
        return None
    df = df.rename(columns={
        0: "survey_date", 1: "fips_state", 2: "fips_county_3",
        7: "u1", 10: "u2", 13: "u34", 16: "u5plus",
    })
    df["fips_county"] = (
        df["fips_state"].astype(int).astype(str).str.zfill(2)
        + df["fips_county_3"].astype(int).astype(str).str.zfill(3)
    )
    # survey_date is YYYYMM
    df["period"] = pd.to_datetime(df["survey_date"].astype(str), format="%Y%m", errors="coerce").dt.date
    df["units_total"] = df[["u1", "u2", "u34", "u5plus"]].fillna(0).sum(axis=1)
    out = df.rename(columns={
        "u1": "units_1unit",
        "u2": "units_2unit",
        "u34": "units_3to4",
        "u5plus": "units_5plus",
    })[["fips_county", "period", "units_total", "units_1unit", "units_2unit", "units_3to4", "units_5plus"]]
    out = out.dropna(subset=["period"])
    return out


def load(con: duckdb.DuckDBPyConnection, refresh: bool = False, months_back: int = 12) -> int:
    """Pull the last `months_back` monthly cumulative county files."""
    today = date.today()
    total = 0
    for offset in range(months_back):
        m = today.month - offset
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        url = _build_url(y, m)
        try:
            path = download(url, suffix=".txt", refresh=refresh)
        except Exception:
            continue
        df = _parse(path)
        if df is not None and not df.empty:
            total += upsert_df(con, "census_permits", df)
    return total
