"""IRS SOI county-to-county migration data.

Annual data on tax filers moving between counties, with adjusted gross
income attached. Inflow/outflow files. The killer dataset for "where is
money moving to."

Format: ZIP archive containing multiple state CSVs. We grab the most
recent published year and parse the inflow/outflow rollups.
"""
from __future__ import annotations
import zipfile
import io
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

# IRS publishes per migration year. 2122 means inflows reported in tax year
# filings for 2021→2022 moves. Update the year as new data ships.
DEFAULT_YEAR_TAG = "2122"


def _build_urls(tag: str) -> dict[str, str]:
    base = "https://www.irs.gov/pub/irs-soi"
    return {
        "inflow": f"{base}/{tag}migrationdata.zip",
    }


def _parse_csv_bytes(name: str, raw: bytes, direction: str, period: str) -> pd.DataFrame | None:
    if not name.lower().endswith(".csv"):
        return None
    try:
        df = pd.read_csv(io.BytesIO(raw), encoding="latin-1", low_memory=False)
    except Exception:
        return None
    df.columns = [c.strip().lower() for c in df.columns]
    # Schema varies slightly across years. Required columns:
    needed = {"y2_statefips", "y2_countyfips", "y1_statefips", "y1_countyfips", "n1", "n2", "agi"}
    if not needed.issubset(set(df.columns)):
        return None
    df = df[
        df["y2_statefips"].astype(str).str.isnumeric()
        & df["y1_statefips"].astype(str).str.isnumeric()
    ]
    df["fips_county"] = (
        df["y2_statefips"].astype(int).astype(str).str.zfill(2)
        + df["y2_countyfips"].astype(int).astype(str).str.zfill(3)
    )
    df["counterparty_fips"] = (
        df["y1_statefips"].astype(int).astype(str).str.zfill(2)
        + df["y1_countyfips"].astype(int).astype(str).str.zfill(3)
    )
    df["period"] = period
    df["direction"] = direction
    out = df[["period", "direction", "fips_county", "counterparty_fips", "n1", "n2", "agi"]].rename(
        columns={"n1": "returns", "n2": "exemptions", "agi": "agi_thousands"}
    )
    return out


def load(con: duckdb.DuckDBPyConnection, year_tag: str = DEFAULT_YEAR_TAG, refresh: bool = False) -> int:
    period = f"20{year_tag[:2]}-20{year_tag[2:]}"
    urls = _build_urls(year_tag)
    total = 0
    for direction, url in urls.items():
        try:
            path = download(url, suffix=".zip", refresh=refresh)
        except Exception as e:
            print(f"IRS {direction} {period}: download failed ({e}) — skipping")
            continue
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    raw = f.read()
                # Each file is "stateXXin.csv" or "stateXXout.csv"; we keep the
                # rollup for the listed direction.
                base = name.lower()
                if "in" in base and direction == "inflow":
                    parsed = _parse_csv_bytes(name, raw, "inflow", period)
                elif "out" in base and direction == "outflow":
                    parsed = _parse_csv_bytes(name, raw, "outflow", period)
                else:
                    parsed = None
                if parsed is not None and not parsed.empty:
                    total += upsert_df(con, "irs_migration", parsed)
    return total
