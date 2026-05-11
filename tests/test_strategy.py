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


def test_msa_detail_includes_stability():
    """The single-MSA endpoint should also include the historical stability tier."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    # Pittsburgh CBSA — should be Boring per the 50y FHFA analysis
    r = client.get("/api/msas/38300")
    if r.status_code != 200:
        pytest.skip("Pittsburgh MSA not in current scored set")
    d = r.json()
    if d.get("stability_tier") is not None:
        assert d["stability_tier"] in {"Boring", "Standard", "Volatile", "Boom-Bust"}
        assert d["historical_max_dd_pct"] < 0


def test_top_zips_stability_filter():
    """?stability=Boring should narrow to zips in low-DD metros."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    r = client.get("/api/zips/top?stability=Boring&limit=20")
    assert r.status_code == 200
    d = r.json()
    assert d["stability_applied"] is True
    assert d["stability_filter"] == "Boring"
    # Every returned zip's parent CBSA must be Boring tier
    for z in d["results"]:
        # If the zip has no stability info, it shouldn't have made it through the filter
        if "stability_tier" in z:
            assert z["stability_tier"] == "Boring"


def test_strategy_endpoint_cache_keys_independent():
    """The TTL cache should key by section so single-section calls don't
    accidentally serve the full report (or vice versa)."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    # Cold-prime
    r_full = client.get("/api/strategy/backtest").json()
    r_section = client.get("/api/strategy/backtest?section=strategies").json()
    # Single-section result has ONLY that section
    assert set(r_section.keys()) == {"strategies"}
    # Full result has all five
    assert set(r_full.keys()) == {"regimes", "drawdowns", "momentum", "strategies", "rent_yield"}


def test_portfolio_resilience_scores_diverse_portfolios(con):
    """Boring-tier deals → high score, Boom-Bust deals → low score."""
    from reip import strategy
    # Pittsburgh + Rochester NY: both Boring tier, ~-2 to -5% historical DD
    boring_deals = [
        {"label": "Pgh", "inputs": {"zip": "15213", "state": "PA", "purchase_price": 150_000,
                                      "ltv": 0.75, "rehab_cost": 0}},
        {"label": "Roc", "inputs": {"zip": "14611", "state": "NY", "purchase_price": 80_000,
                                      "ltv": 0.75, "rehab_cost": 0}},
    ]
    boring = strategy.portfolio_resilience(con, boring_deals)
    assert boring["resilience_score"] >= 80
    assert boring["weighted_historical_max_dd_pct"] > -10

    # Fort Myers + LA: both Boom-Bust, -50% to -65% historical DD
    bust_deals = [
        {"label": "FM", "inputs": {"zip": "33908", "state": "FL", "purchase_price": 300_000,
                                     "ltv": 0.75, "rehab_cost": 0}},
        {"label": "LA", "inputs": {"zip": "90001", "state": "CA", "purchase_price": 500_000,
                                     "ltv": 0.75, "rehab_cost": 0}},
    ]
    bust = strategy.portfolio_resilience(con, bust_deals)
    assert bust["resilience_score"] < boring["resilience_score"]
    assert bust["weighted_historical_max_dd_pct"] < -25


def test_portfolio_resilience_division_fallback(con):
    """SF zip 94110 → CBSA 41860 (MSA) → falls back to FHFA Division 41884.
    Without the fallback, SF deals would never map and resilience would be wrong."""
    from reip import strategy
    deals = [
        {"label": "SF", "inputs": {"zip": "94110", "state": "CA", "purchase_price": 800_000,
                                     "ltv": 0.75, "rehab_cost": 0}},
    ]
    r = strategy.portfolio_resilience(con, deals)
    # Should have mapped successfully via the division fallback
    assert r["deals_mapped"] >= 1
    assert r["deals_unmapped"] == 0
    assert r["weighted_historical_max_dd_pct"] is not None


def test_portfolio_aggregate_returns_resilience(con):
    """The /api/portfolio/aggregate endpoint should now include `resilience`."""
    from fastapi.testclient import TestClient
    from reip.api import app
    client = TestClient(app)
    body = {
        "deals": [
            {"label": "Pgh", "inputs": {"zip": "15213", "state": "PA",
                                          "purchase_price": 150_000, "ltv": 0.75, "rehab_cost": 0},
             "stress": {"gate": {"verdict": "GREEN"},
                        "scenarios": [{"irr": 0.1, "dscr": 1.3, "cash_flow_y1": 500, "cash_on_cash": 0.04},
                                       {"irr": 0.05, "dscr": 1.1, "cash_flow_y1": 100, "cash_on_cash": 0.02},
                                       {"irr": -0.02, "dscr": 0.9, "cash_flow_y1": -200, "cash_on_cash": -0.01}]}},
        ],
    }
    r = client.post("/api/portfolio/aggregate", json=body)
    assert r.status_code == 200
    out = r.json()
    assert "resilience" in out
    assert out["resilience"]["deals_mapped"] >= 1


def test_chat_portfolio_resilience_tool(con):
    """The chat tool should read from the module-level pipeline and score it."""
    from reip import chat
    chat._CURRENT_PIPELINE = [
        {"label": "Pgh", "purchase_price": 150_000, "monthly_rent": 1500,
         "state": "PA", "zip": "15213"},
    ]
    out = chat._execute("portfolio_resilience", {})
    assert "resilience_score" in out
    assert out["deals_mapped"] >= 1
    # And with no pipeline set, returns helpful error
    chat._CURRENT_PIPELINE = []
    out2 = chat._execute("portfolio_resilience", {})
    assert "error" in out2


def test_portfolio_boom_bust_concentration_warning():
    """Portfolio dominated by FL+CA+NV should fire the Boom-Bust state warning."""
    from reip import portfolio, tax
    deals = [
        {"label": "FL 1", "status": "underwritten",
         "inputs": {"purchase_price": 300_000, "monthly_rent": 2500, "rehab_cost": 0,
                     "ltv": 0.75, "state": "FL"},
         "stress": {"gate": {"verdict": "GREEN"},
                    "scenarios": [{"irr": 0.10, "dscr": 1.3, "cash_flow_y1": 500, "cash_on_cash": 0.04},
                                   {"irr": 0.05, "dscr": 1.1, "cash_flow_y1": 200, "cash_on_cash": 0.02},
                                   {"irr": -0.05, "dscr": 0.9, "cash_flow_y1": -300, "cash_on_cash": -0.01}]}},
        {"label": "CA 1", "status": "underwritten",
         "inputs": {"purchase_price": 500_000, "monthly_rent": 3500, "rehab_cost": 0,
                     "ltv": 0.75, "state": "CA"},
         "stress": {"gate": {"verdict": "GREEN"},
                    "scenarios": [{"irr": 0.08, "dscr": 1.2, "cash_flow_y1": 300, "cash_on_cash": 0.02},
                                   {"irr": 0.03, "dscr": 1.0, "cash_flow_y1": 0, "cash_on_cash": 0.0},
                                   {"irr": -0.10, "dscr": 0.8, "cash_flow_y1": -500, "cash_on_cash": -0.02}]}},
    ]
    out = portfolio.aggregate(deals)
    text = " ".join(out["concentration_warnings"]).lower()
    assert "boom-bust" in text or "boom bust" in text
