"""Tests for the Tier 1 / Tier 2 additions (snapshots, freshness,
backtest, report, render, underwriting math)."""
from __future__ import annotations
import duckdb
import pandas as pd
from datetime import datetime, timedelta
from reip.store import init, upsert_df
from reip import snapshots, freshness, render, report, underwriting


def _make_scored_fixture():
    return pd.DataFrame([
        {"cbsa_code": "32820", "cbsa_name": "Memphis, TN-MS-AR", "archetype": "Cashflow Heartland",
         "appreciation_score": -0.10, "cashflow_score":  0.10, "total_return_score": 0.00},
        {"cbsa_code": "12420", "cbsa_name": "Austin, TX",         "archetype": "Mixed",
         "appreciation_score":  0.15, "cashflow_score": -0.10, "total_return_score": 0.025},
        {"cbsa_code": "39580", "cbsa_name": "Raleigh-Cary, NC",    "archetype": "Sun Belt Growth",
         "appreciation_score":  0.12, "cashflow_score": -0.04, "total_return_score": 0.04},
    ])


def test_snapshot_and_diff():
    con = duckdb.connect(":memory:")
    init(con)
    df = _make_scored_fixture()
    t1 = datetime(2026, 5, 1, 12, 0, 0)
    snapshots.snapshot(con, df, ts=t1)
    # Move Memphis up, Raleigh down for the second snapshot
    df2 = df.copy()
    df2.loc[df2.cbsa_code == "32820", "total_return_score"] = 0.20
    df2.loc[df2.cbsa_code == "39580", "total_return_score"] = -0.05
    snapshots.snapshot(con, df2, ts=t1 + timedelta(days=7))
    diff = snapshots.diff(con, by="total", top_movers=10)
    assert not diff.empty
    # Memphis must show movement up (rank improved)
    memphis = diff[diff.cbsa_code == "32820"].iloc[0]
    assert memphis["rank_now"] < memphis["rank_then"]


def test_freshness_stamping():
    con = duckdb.connect(":memory:")
    init(con)
    freshness.stamp(con, "zillow", 1000)
    rows = freshness.status(con)
    z = next(r for r in rows if r["source_name"] == "zillow")
    assert z["rows_loaded"] == 1000
    assert z["last_refresh"] is not None
    # And a never-loaded source shows up as never
    f = next(r for r in rows if r["source_name"] == "fred")
    assert f["last_refresh"] is None
    # stale_sources includes the never-loaded ones
    stale = freshness.stale_sources(con)
    assert "fred" in stale
    assert "zillow" not in stale


def test_underwriting_math_brrrr():
    a = underwriting.Assumptions(
        purchase_price=70_000, rehab_cost=25_000, arv=130_000,
        monthly_rent=1200, mortgage_rate=0.075,
    )
    out = underwriting.underwrite(a)
    pf = out["proforma_y1"]
    # cap rate ≈ NOI / price; for $1200/mo at 5% vac and 40% opex = $14400 EGI
    # → opex 5760 + tax 840 + insurance 1500 = 8100 → NOI ~$6300
    assert pf["cap_rate"] > 0.07
    assert pf["dscr"] > 1.0
    brrrr = out["brrrr_refi"]
    assert brrrr["applicable"] is True
    # ARV $130k * 75% = $97.5k new loan vs $70k * 75% = $52.5k payoff = $45k cash
    assert abs(brrrr["cash_out_at_refi"] - 45000) < 1
    # all-in $70k*1.03 + $25k = $97.1k, new loan $97.5k → equity left in 0
    assert brrrr["equity_left_in_after_refi"] == 0
    assert brrrr["infinite_return"] is True


def test_underwriting_math_bad_deal():
    """A $280k SFR at $2200/mo and 7% rates should fail DSCR."""
    a = underwriting.Assumptions(
        purchase_price=280_000, monthly_rent=2200, mortgage_rate=0.07,
    )
    pf = underwriting.proforma(a)
    assert pf["dscr"] < 1.0
    assert pf["cash_flow_y1"] < 0


def test_render_helpers_no_throw():
    # Sparkline should handle NaN and empty
    assert render._spark([]) == ""
    assert render._spark([1.0, 2.0, 3.0]) != ""
    # Color score returns a styled string
    assert "green" in render._color_score(0.30)
    assert "red" in render._color_score(-0.30)
    assert "—" in render._color_score(float("nan"))


def test_html_report_self_contained(tmp_path):
    df = _make_scored_fixture()
    df["pop"] = [1_300_000, 2_300_000, 1_400_000]
    df["pop_cagr_5yr"] = [-0.001, 0.027, 0.022]
    df["net_migration_pct_pop"] = [0.010, 0.039, 0.040]
    df["gross_yield"] = [0.078, 0.048, 0.055]
    df["permits_per_1000_hh"] = [1.96, 7.23, 8.64]
    df["completeness"] = [0.79, 0.79, 0.79]
    out = report.build(df, tmp_path / "r.html")
    text = out.read_text()
    # Single file. JS underwriting calculator is inlined.
    assert "function underwrite" in text
    assert "function amortizingPayment" in text
    # Every MSA shows up in the embedded payload
    for code in ["32820", "12420", "39580"]:
        assert code in text
    # No external script tags (the page should run from disk with no network)
    assert "<script src" not in text
