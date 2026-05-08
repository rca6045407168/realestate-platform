"""Source freshness + cost-aware refresh.

LiteLLM analog: instead of routing model calls, we route data refreshes —
free public sources first, paid only when their cache is stale.
"""
from __future__ import annotations
from datetime import datetime
import duckdb
import pandas as pd
from .store import upsert_df

# Each source declares its expected refresh cadence.
# Reip refreshes a source only when age > cadence_days.
CADENCE = {
    "zip_xwalk":   3650,   # static, refresh every ~10y
    "cbsa_xwalk":  365,
    "static_data": 365,
    "zillow":      30,     # monthly
    "redfin":      7,      # weekly
    "irs":         365,    # annual
    "permits":     30,     # monthly
    "fema":        90,
    "fred":        30,
    "hud":         365,
    "bls":         90,     # quarterly
    "acs":         365,
    "fhfa":        90,     # quarterly
}


def stamp(con: duckdb.DuckDBPyConnection, source_name: str, rows: int) -> None:
    df = pd.DataFrame([{
        "source_name": source_name,
        "last_refresh": datetime.now(),
        "rows_loaded": int(rows),
        "expected_cadence_days": CADENCE.get(source_name, 30),
    }])
    upsert_df(con, "source_freshness", df)


def status(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute("SELECT * FROM source_freshness ORDER BY source_name").df()
    out = rows.to_dict("records")
    seen = {r["source_name"] for r in out}
    for src in CADENCE:
        if src not in seen:
            out.append({
                "source_name": src,
                "last_refresh": None,
                "rows_loaded": 0,
                "expected_cadence_days": CADENCE[src],
            })
    return sorted(out, key=lambda r: r["source_name"])


def stale_sources(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return the names of sources that are past their cadence."""
    now = datetime.now()
    out = []
    for r in status(con):
        last = r.get("last_refresh")
        cadence = r.get("expected_cadence_days") or 30
        if last is None:
            out.append(r["source_name"])
        else:
            age_days = (now - last).days
            if age_days > cadence:
                out.append(r["source_name"])
    return out
