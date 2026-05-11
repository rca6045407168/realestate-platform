"""Strategy module — pin the contract on the 50y empirical backtests.

These run against live data so the numbers will drift slightly as ingests
refresh; tests assert ranges and invariants, not exact values."""
from __future__ import annotations
import pytest
from reip.store import connect
from reip import strategy


@pytest.fixture(scope="module")
def con():
    return connect()


def test_regime_decomposition_shape(con):
    r = strategy.regime_decomposition(con)
    assert len(r) == 8
    for row in r:
        if "median_cagr" in row:
            assert "best_metro" in row and "worst_metro" in row
            assert row["best_cagr"] >= row["median_cagr"] >= row["worst_cagr"]


def test_drawdown_panel_invariants(con):
    d = strategy.drawdown_panel(con, top_n=15)
    assert d["n_metros"] > 100
    # Median DD should be negative and reasonable
    assert -50 < d["median_max_dd_pct"] < 0
    # Worst metros are deeper than median
    assert d["worst"][0]["max_dd_pct"] < d["median_max_dd_pct"]
    # Best (shallowest) are less deep than median
    assert d["best"][-1]["max_dd_pct"] > d["median_max_dd_pct"]
    # Pittsburgh historically has DD < 5%
    pit_in_best = any("Pittsburgh" in r["name"] for r in d["best"])
    assert pit_in_best, "Pittsburgh should appear in best-DD list"


def test_momentum_persistence_signal(con):
    m = strategy.momentum_persistence(con, window_years=3)
    assert m["n_transitions"] > 5000
    # Top-Q stays top should beat random (25%)
    assert m["p_top_stays_top"] > 0.30
    # Forward 3y return by quartile — Q1 should beat Q4
    by_q = m["fwd_returns_by_quartile"]
    q1 = next(r for r in by_q if r["past_quartile"] == 1)
    q4 = next(r for r in by_q if r["past_quartile"] == 4)
    assert q1["mean_fwd_return"] > q4["mean_fwd_return"]
    # The spread should be material (> 3pp on average)
    assert m["top_minus_bottom_fwd_return"] > 0.03


def test_strategy_backtest_runs_all_four(con):
    r = strategy.strategy_backtest(con)
    names = {s["strategy"] for s in r}
    assert names == {"All-Weather", "CA Coastal", "Sun Belt Growth", "Heartland Yield"}
    for s in r:
        if "error" in s:
            continue
        assert s["holding_multiple"] > 0
        assert -1 < s["cagr"] < 0.5
        assert s["max_dd_pct"] is None or s["max_dd_pct"] < 0


def test_strategy_sun_belt_beats_ca_coastal(con):
    """Empirical claim from STRATEGY.md: SunBelt CAGR > CA Coastal CAGR over 34y."""
    r = {s["strategy"]: s for s in strategy.strategy_backtest(con)}
    if "error" in r["Sun Belt Growth"] or "error" in r["CA Coastal"]:
        pytest.skip("missing data for one of the strategies")
    assert r["Sun Belt Growth"]["cagr"] > r["CA Coastal"]["cagr"]


def test_rent_yield_correlation_negative_but_weak(con):
    """Yield should negatively correlate with appreciation, but only weakly."""
    r = strategy.rent_yield_panel(con)
    assert r["n_metros"] > 100
    corr = r["corr_yield_vs_growth"]
    assert -0.6 < corr < 0.1, f"Yield-growth correlation unexpected: {corr}"


def test_full_report_json_serializable(con):
    import json
    r = strategy.full_report(con)
    # Should round-trip JSON without errors
    s = json.dumps(r, default=str)
    assert len(s) > 1000


def test_endpoint_section_filters(con):
    """The API endpoint should support section= filtering."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    for section in ["regimes", "drawdowns", "momentum", "strategies", "rent_yield"]:
        r = client.get(f"/api/strategy/backtest?section={section}")
        assert r.status_code == 200
        d = r.json()
        assert section in d
    # Full report
    r = client.get("/api/strategy/backtest")
    assert r.status_code == 200
    d = r.json()
    assert set(d.keys()) == {"regimes", "drawdowns", "momentum", "strategies", "rent_yield"}


def test_stability_panel_assigns_tiers(con):
    """Every metro with enough data gets one of 4 tiers."""
    from reip import strategy
    panel = strategy.compute_stability_panel(con)
    assert len(panel) > 100
    tiers = {v["tier"] for v in panel.values()}
    assert tiers <= {"Boring", "Standard", "Volatile", "Boom-Bust"}
    # Pittsburgh historically: should be Boring
    pit_codes = [c for c, v in panel.items() if v["max_dd_pct"] > -2.5]
    # At least Pittsburgh + Springfield IL show up in <-2.5% DD
    assert len(pit_codes) >= 1


def test_msa_endpoint_carries_stability_tier(con):
    """The /api/msas response should include stability columns now."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    r = client.get("/api/msas?limit=20")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) > 0
    # Some rows should have a stability tier (not all, since the panel
    # only covers metros with FHFA HPI history)
    with_tier = [m for m in rows if m.get("stability_tier")]
    assert len(with_tier) > 0


def test_msa_endpoint_stability_filter(con):
    """Filter ?stability=Boring should return only Boring-tier metros."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    r = client.get("/api/msas?stability=Boring&limit=20")
    assert r.status_code == 200
    rows = r.json()
    # Either empty (no Boring in top metros) or all match
    for m in rows:
        assert m["stability_tier"] == "Boring"


def test_chat_strategy_backtest_tool_executes():
    """The chat tool should execute and return real data."""
    from reip import chat
    out = chat._execute("strategy_backtest", {"section": "strategies"})
    assert "strategies" in out
    strategies = out["strategies"]
    assert len(strategies) == 4
    names = {s["strategy"] for s in strategies}
    assert names == {"All-Weather", "CA Coastal", "Sun Belt Growth", "Heartland Yield"}
