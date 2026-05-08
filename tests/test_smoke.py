"""Minimal smoke tests — schema init + score query shape.

Doesn't hit network; uses an in-memory duckdb.
"""
from __future__ import annotations
import duckdb
from reip.store import init, upsert_df, SCHEMA
from reip import score
import pandas as pd


def test_schema_init():
    con = duckdb.connect(":memory:")
    init(con)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    expected = {
        "zillow_zhvi", "zillow_zori", "redfin_market", "irs_migration",
        "census_permits", "bls_qcew", "hud_fmr", "fema_nfip", "fred_macro",
        "zip_county_xwalk", "redfin_listings",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_upsert_idempotent():
    con = duckdb.connect(":memory:")
    init(con)
    df = pd.DataFrame({"zip": ["10001"], "period": [pd.Timestamp("2026-01-01").date()], "value": [1000.0]})
    n1 = upsert_df(con, "zillow_zhvi", df)
    n2 = upsert_df(con, "zillow_zhvi", df)
    count = con.execute("SELECT COUNT(*) FROM zillow_zhvi").fetchone()[0]
    assert n1 == 1 and n2 == 1 and count == 1


def test_score_handles_empty():
    df = pd.DataFrame(columns=[
        "zip", "zhvi", "zori", "zhvi_yr_ago", "yoy_appreciation", "gross_yield",
        "dom", "sale_to_list", "inventory", "permits_12mo", "in_agi", "out_agi",
        "net_agi_inflow", "flood_claims_total", "flood_paid_total", "emp", "wage",
    ])
    out = score.score(df)
    assert "score" in out.columns
    assert "completeness" in out.columns
