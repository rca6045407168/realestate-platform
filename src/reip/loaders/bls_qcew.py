"""BLS Quarterly Census of Employment & Wages (QCEW) — county-level annual.

The per-county API at data.bls.gov/cew/data/api/<yr>/<q>/area/<fips>.csv
requires ~12,000 fetches for full national coverage, which is impractical.
We instead download the annual-by-area bulk zip (~130 MB / year), which
contains one CSV per area with every industry/ownership/quarter combination.
For MSA scoring we keep only:
  - agglvl_code = 70  (county-level rollup)
  - own_code     = 0  (total covered, all ownerships)
  - industry_code = '10' (total all industries)
and stamp it into bls_qcew with period='YYYY-Annual'.

We pull two years (default 2019 and 2024) so the score can compute
5-yr employment CAGR.
"""
from __future__ import annotations
import io
import zipfile
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

BASE = "https://data.bls.gov/cew/data/files/{year}/csv/{year}_annual_by_area.zip"


def _load_year(con: duckdb.DuckDBPyConnection, year: int, refresh: bool = False) -> int:
    url = BASE.format(year=year)
    try:
        path = download(url, suffix=f".bls{year}.zip", refresh=refresh)
    except Exception as e:
        print(f"BLS {year}: download failed ({e})")
        return 0
    total = 0
    keep_chunks = []
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        # Each per-area CSV has the same schema; we filter and concat.
        for name in names:
            with zf.open(name) as f:
                try:
                    df = pd.read_csv(
                        f, low_memory=False,
                        dtype={"area_fips": str, "industry_code": str,
                               "own_code": str, "agglvl_code": str},
                    )
                except Exception:
                    continue
            df = df[
                (df["agglvl_code"] == "70")
                & (df["own_code"] == "0")
                & (df["industry_code"] == "10")
            ]
            if df.empty:
                continue
            keep_chunks.append(df)
    if not keep_chunks:
        return 0
    big = pd.concat(keep_chunks, ignore_index=True)
    big["fips_county"] = big["area_fips"].str.zfill(5)
    big["period"] = f"{year}-Annual"
    out = big.rename(columns={
        "annual_avg_emplvl": "employment",
        "total_annual_wages": "total_wages",
        "annual_avg_wkly_wage": "avg_weekly_wage",
        "annual_avg_estabs": "qtrly_estabs",   # named differently in annual file
    })
    cols = ["fips_county", "period", "industry_code",
            "employment", "total_wages", "avg_weekly_wage", "qtrly_estabs"]
    out = out[[c for c in cols if c in out.columns]]
    return upsert_df(con, "bls_qcew", out)


def load(
    con: duckdb.DuckDBPyConnection,
    years: list[int] | None = None,
    refresh: bool = False,
    **_,
) -> int:
    """Pull the annual-by-area bulk dump for each year. Default endpoints
    are 5 years apart (2019 + 2024) so msa_score can compute 5-yr CAGR."""
    years = years or [2019, 2024]
    total = 0
    for y in years:
        n = _load_year(con, y, refresh=refresh)
        print(f"  BLS {y}: {n} rows")
        total += n
    return total
