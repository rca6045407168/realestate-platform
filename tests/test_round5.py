"""Round-5 features: data freshness, sales-based ARV, rate sensitivity,
pipeline-aware Top Zips. Pin the contracts so a future refactor doesn't
silently break them."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from reip.api import app
from reip.store import connect
from reip import buybox, stress, underwriting as uw


client = TestClient(app)


# ---- Data freshness -------------------------------------------------------

def test_freshness_endpoint_returns_all_expected_sources():
    r = client.get("/api/freshness")
    assert r.status_code == 200
    d = r.json()
    sources = {s["source"] for s in d["sources"]}
    # All the tables the ranking engine depends on must be in the report
    assert "zillow_zhvi" in sources
    assert "zillow_zori" in sources
    assert "redfin_market" in sources
    assert "fema_nfip" in sources
    assert "acs_county" in sources


def test_freshness_flags_per_source_age():
    r = client.get("/api/freshness")
    d = r.json()
    for s in d["sources"]:
        if "error" in s:
            continue
        assert "stale" in s
        assert "days_since" in s


def test_freshness_top_level_summary():
    r = client.get("/api/freshness")
    d = r.json()
    assert isinstance(d["any_stale"], bool)
    assert d["stale_count"] >= 0


# ---- Sales-based ARV ------------------------------------------------------

def test_sales_based_arv_returns_real_transactions():
    """KCMO 64120 has at least some redfin_market rows."""
    con = connect()
    out = buybox.arv_sales_based(con, "64120")
    # It's possible there are zero sales in the very recent window;
    # the function returns None in that case. Test the contract more
    # carefully — if we DO get data back, it should be well-formed.
    if out is None:
        pytest.skip("No redfin_market data for KCMO 64120 in trailing window.")
    assert out["arv"] > 0
    assert out["homes_sold_trailing"] >= 1
    assert "method" in out
    assert "Redfin" in out["method"]


def test_buybox_includes_sales_based_arv_when_available():
    """The full buy-box payload should attach arv_sales_based for zips with data."""
    con = connect()
    b = buybox.derive(con, "33908")  # Lee County FL — high volume
    assert b is not None
    # Fort Myers has 800+ sales/mo → must include sales_based
    assert b.arv_sales_based is not None
    # And the 12mo change should be visible — Ian zone is currently negative
    if b.arv_sales_based.get("chg_12mo") is not None:
        # Just verify it's a sensible float
        assert -1 < b.arv_sales_based["chg_12mo"] < 1


def test_arv_endpoint_returns_sales_based_when_present():
    r = client.get("/api/zips/33908/arv")
    assert r.status_code == 200
    d = r.json()
    if "sales_based" in d:
        assert d["sales_based"]["arv"] > 0
        assert d["sales_based"]["lookback_months"] >= 1


# ---- Rate sensitivity -----------------------------------------------------

def test_rate_curve_returns_band():
    a = uw.Assumptions(purchase_price=80_000, monthly_rent=1700,
                        rehab_cost=5000, mortgage_rate=0.07)
    r = stress.rate_sensitivity(a, state=None)
    assert len(r) >= 6
    rates = [p["rate"] for p in r]
    assert rates == sorted(rates)
    for p in r:
        assert "verdict" in p
        assert p["verdict"] in {"GREEN", "YELLOW", "RED"}
        assert "base_irr" in p
        assert "base_dscr" in p


def test_rate_curve_monotone_in_dscr():
    """As rate climbs, DSCR should not improve (interest cost rises)."""
    a = uw.Assumptions(purchase_price=80_000, monthly_rent=1700,
                        rehab_cost=5000, mortgage_rate=0.07)
    r = stress.rate_sensitivity(a, state=None)
    dscrs = [p["base_dscr"] for p in r if p["base_dscr"] is not None]
    for i in range(1, len(dscrs)):
        assert dscrs[i] <= dscrs[i - 1] + 1e-6


def test_stress_test_includes_rate_curve_by_default():
    a = uw.Assumptions(purchase_price=80_000, monthly_rent=1700, rehab_cost=5000)
    r = stress.stress_test(a)
    assert "rate_curve" in r
    assert len(r["rate_curve"]) >= 6


def test_stress_test_rate_curve_disable():
    a = uw.Assumptions(purchase_price=80_000, monthly_rent=1700, rehab_cost=5000)
    r = stress.stress_test(a, include_rate_curve=False)
    assert "rate_curve" not in r


# ---- Pipeline-aware Top Zips ---------------------------------------------

def test_top_zips_concentrated_states_param_accepted():
    r = client.get("/api/zips/top?limit=10&concentrated_states=MO,OH")
    assert r.status_code == 200
    d = r.json()
    assert d["diversify_applied"] is True
    assert set(d["concentrated_states"]) == {"MO", "OH"}
    assert d["diversify_penalty"] == 0.30


def test_top_zips_no_concentrated_states_is_passthrough():
    r = client.get("/api/zips/top?limit=10")
    assert r.status_code == 200
    d = r.json()
    assert d["diversify_applied"] is False
    assert d["concentrated_states"] == []


def test_top_zips_diversify_actually_demotes():
    """Compare top-3 with and without diversify; if MO is in concentrated,
    the no-diversify top should over-represent MO more than diversified."""
    r1 = client.get("/api/zips/top?limit=10&sort=regime").json()
    r2 = client.get("/api/zips/top?limit=10&sort=regime&concentrated_states=MO").json()
    mo_count_no_div = sum(1 for z in r1["results"] if z["state"] == "Missouri")
    mo_count_div    = sum(1 for z in r2["results"] if z["state"] == "Missouri")
    # Diversified should have ≤ as many MO zips. May be equal if MO doesn't
    # dominate the top-10 to begin with, but should never increase.
    assert mo_count_div <= mo_count_no_div
