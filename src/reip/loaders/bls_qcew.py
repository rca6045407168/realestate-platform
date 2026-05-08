"""BLS Quarterly Census of Employment & Wages (QCEW) — county.

Public per-quarter county/industry aggregates. URL pattern:
  https://data.bls.gov/cew/data/api/<year>/<quarter>/area/<fips>.csv

We pull the most recent published quarter for each requested county FIPS.
By default we hit the all-industries rollup (industry_code = "10").
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

BASE = "https://data.bls.gov/cew/data/api"


def _url(year: int, q: int, fips: str) -> str:
    return f"{BASE}/{year}/{q}/area/{fips}.csv"


def load(
    con: duckdb.DuckDBPyConnection,
    fips_list: list[str] | None = None,
    year: int = 2025,
    quarters: tuple[int, ...] = (1, 2, 3, 4),
    industry_filter: tuple[str, ...] = ("10",),  # 10 = total all industries
    refresh: bool = False,
) -> int:
    """Pull QCEW for the listed counties. If fips_list is None, the caller
    is expected to populate `zip_county_xwalk` first; we'll pull every
    distinct county there."""
    if fips_list is None:
        try:
            fips_list = [r[0] for r in con.execute("SELECT DISTINCT fips_county FROM zip_county_xwalk").fetchall()]
        except Exception:
            fips_list = []
    if not fips_list:
        return 0
    total = 0
    for fips in fips_list:
        for q in quarters:
            url = _url(year, q, fips)
            try:
                path = download(url, suffix=".csv", refresh=refresh)
                df = pd.read_csv(path, dtype={"area_fips": str, "industry_code": str})
            except Exception:
                continue
            df = df[df["industry_code"].isin(industry_filter)]
            if df.empty:
                continue
            df["fips_county"] = df["area_fips"].str.zfill(5)
            df["period"] = f"{year}-Q{q}"
            df = df.rename(columns={
                "month3_emplvl": "employment",
                "total_qtrly_wages": "total_wages",
                "avg_wkly_wage": "avg_weekly_wage",
                "qtrly_estabs_count": "qtrly_estabs",
            })
            keep = df[["fips_county", "period", "industry_code", "employment", "total_wages", "avg_weekly_wage", "qtrly_estabs"]]
            total += upsert_df(con, "bls_qcew", keep)
    return total
