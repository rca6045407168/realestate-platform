"""County → CBSA (MSA/μSA) crosswalk from Census delineation files.

Public Excel file. We parse the "List 1" sheet which has one row per county
with its containing CBSA code and title.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import download
from ..store import upsert_df

# Most recent OMB delineation. URL pattern stable; bump year as needed.
URL = "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx"


def load(con: duckdb.DuckDBPyConnection, refresh: bool = False) -> int:
    path = download(URL, suffix=".xlsx", refresh=refresh)
    # Header is on row 3 (0-indexed 2).
    df = pd.read_excel(path, header=2, dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_").replace("/", "_") for c in df.columns]
    # Standard columns: cbsa_code, cbsa_title, metropolitan_micropolitan_statistical_area,
    # state_name, fips_state_code, fips_county_code, county_county_equivalent
    state_col = next((c for c in df.columns if c.startswith("fips_state")), None)
    cty_col = next((c for c in df.columns if c.startswith("fips_county")), None)
    cbsa_code_col = next((c for c in df.columns if "cbsa" in c and "code" in c), None)
    cbsa_title_col = next((c for c in df.columns if "cbsa" in c and "title" in c), None)
    type_col = next((c for c in df.columns if "metropolitan" in c and "micropolitan" in c), None)
    state_name_col = next((c for c in df.columns if "state_name" in c), None)
    if not all([state_col, cty_col, cbsa_code_col, cbsa_title_col]):
        raise RuntimeError(f"Could not locate columns in {df.columns.tolist()}")
    out = pd.DataFrame({
        "fips_county": df[state_col].str.zfill(2) + df[cty_col].str.zfill(3),
        "cbsa_code": df[cbsa_code_col],
        "cbsa_name": df[cbsa_title_col],
        "cbsa_type": df[type_col] if type_col else "",
        "state": df[state_name_col] if state_name_col else "",
    })
    out = out.dropna(subset=["fips_county", "cbsa_code"]).drop_duplicates("fips_county")
    return upsert_df(con, "county_cbsa_xwalk", out)
