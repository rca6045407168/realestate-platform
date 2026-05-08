"""FEMA OpenFEMA: NFIP claims by county-year.

Tells you which counties actually pay flood claims. Combined with mortgage
+ insurance trends, this is the "left-tail" risk overlay.

Public, no auth. We aggregate the NFIPClaims dataset by county+year of loss.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ._http import get_json
from ..store import upsert_df

BASE = "https://www.fema.gov/api/open/v2/FimaNfipClaims"


def load(con: duckdb.DuckDBPyConnection, since_year: int = 2015) -> int:
    rows = []
    skip = 0
    page_size = 5000
    # We fetch only aggregate fields; OData supports $select.
    while True:
        url = (
            f"{BASE}?$select=countyCode,yearOfLoss,amountPaidOnBuildingClaim,amountPaidOnContentsClaim"
            f"&$filter=yearOfLoss%20ge%20{since_year}"
            f"&$top={page_size}&$skip={skip}"
        )
        try:
            data = get_json(url)
        except Exception as e:
            print(f"FEMA fetch failed at skip={skip}: {e}")
            break
        chunk = data.get("FimaNfipClaims", [])
        if not chunk:
            break
        rows.extend(chunk)
        skip += page_size
        if len(chunk) < page_size:
            break
        # Safety: cap at 200k rows for an initial pull
        if skip >= 200_000:
            break
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["countyCode"] = df["countyCode"].astype(str).str.zfill(5)
    df["paid"] = df.get("amountPaidOnBuildingClaim", 0).fillna(0) + df.get("amountPaidOnContentsClaim", 0).fillna(0)
    agg = df.groupby(["countyCode", "yearOfLoss"], as_index=False).agg(claim_count=("paid", "size"), total_paid=("paid", "sum"))
    agg = agg.rename(columns={"countyCode": "fips_county", "yearOfLoss": "year"})
    return upsert_df(con, "fema_nfip", agg[["fips_county", "year", "claim_count", "total_paid"]])
