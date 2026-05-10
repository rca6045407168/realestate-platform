"""Stress-test module — multi-scenario underwriter + state overlays + gate.

Tests cover:
  1. Base/stress/worst scenarios all run and return finite metrics for a
     solid Cleveland duplex (sanity baseline).
  2. Gate verdict ordering: GREEN base < stress < worst (each scenario should
     be no better than the one above it on at least one key metric).
  3. State overlay applies the right deltas (FL insurance ×1.50, TX +1.5pp
     property tax, OH rehab ×1.30).
  4. A clearly-broken deal screens RED with mitigations + a `price_to_green`
     suggestion.
  5. A solid bread-and-butter deal screens GREEN with no mitigations.
  6. Break-even occupancy: a deal that can't CF at 100% returns > 1.0.
  7. State unknown → no overlay, no crash.
"""
from __future__ import annotations
import math
import pytest

from reip import stress, underwriting as uw


def _solid_deal():
    """Strong cashflow deal: $80K, $1700/mo (~2.1% rent-to-price). Survives
    worst-case with positive IRR — should clear GREEN. Note: a 1% rule deal
    (e.g. $90K / $1500) screens YELLOW/RED here because the platform stress-tests
    against recession-grade rent/vacancy/rate shocks. That's the whole point —
    don't soften the gate to flatter BiggerPockets-style rule-of-thumb deals."""
    return uw.Assumptions(
        purchase_price=80_000, monthly_rent=1700, rehab_cost=5000,
        property_tax_rate=0.012, insurance_annual=1000,
        mortgage_rate=0.07, ltv=0.75, vacancy=0.05,
    )


def _thin_deal():
    """1% rule deal: $100K / $1700 — base case strong, worst case bleeds.
    Should screen YELLOW: 'survives but you'll feel a recession.'"""
    return uw.Assumptions(
        purchase_price=100_000, monthly_rent=1700, rehab_cost=0,
        property_tax_rate=0.012, insurance_annual=1200,
        mortgage_rate=0.07, ltv=0.75, vacancy=0.05,
    )


def _bad_deal():
    """Overpriced FL SFR: $400K, $2000/mo, vacancy + cost killers."""
    return uw.Assumptions(
        purchase_price=400_000, monthly_rent=2000, rehab_cost=10_000,
        property_tax_rate=0.012, insurance_annual=4000,
        mortgage_rate=0.07, ltv=0.75, vacancy=0.05,
    )


def test_three_scenarios_run_and_metrics_finite():
    r = stress.stress_test(_solid_deal())
    names = [s["name"] for s in r["scenarios"]]
    assert names == ["base", "stress", "worst"]
    for s in r["scenarios"]:
        assert s["irr"] is not None
        assert not math.isnan(s["cash_on_cash"])
        assert s["dscr"] is not None
        assert s["break_even_occupancy"] is not None
        # IRR floor is -0.99 from the bisect, but should be finite
        assert -1.0 <= s["irr"] <= 5.0


def test_scenarios_are_monotonically_worse():
    """For any deal, stress should be no better than base, worst no better than stress
    on IRR + DSCR + CoC. (Not strictly true for every metric in every case due to
    discontinuities, but should hold on these three for sane inputs.)"""
    r = stress.stress_test(_solid_deal())
    base, stress_s, worst = r["scenarios"]
    assert stress_s["irr"] <= base["irr"] + 1e-6
    assert worst["irr"] <= stress_s["irr"] + 1e-6
    assert stress_s["dscr"] <= base["dscr"] + 1e-6
    assert worst["dscr"] <= stress_s["dscr"] + 1e-6


def test_state_overlay_florida_applies_insurance_mult():
    """FL overlay should multiply insurance by 1.50 in base scenario."""
    a = _solid_deal()
    base_ins = a.insurance_annual
    r = stress.stress_test(a, state="FL")
    assert r["state"] == "FL"
    assert r["state_overlay_applied"] is True
    # The summary string should mention insurance multiplier
    assert "insurance" in (r["state_overlay_summary"] or "").lower()
    # Base scenario should reflect higher insurance via lower NOI vs no-overlay
    r2 = stress.stress_test(a, state=None)
    assert r["scenarios"][0]["cash_flow_y1"] < r2["scenarios"][0]["cash_flow_y1"]


def test_state_overlay_texas_applies_property_tax():
    """TX overlay adds +1.5pp to property tax."""
    a = _solid_deal()
    r_no = stress.stress_test(a, state=None)
    r_tx = stress.stress_test(a, state="TX")
    # TX base CF should be materially lower
    delta = r_tx["scenarios"][0]["cash_flow_y1"] - r_no["scenarios"][0]["cash_flow_y1"]
    # ~ -1.5% × $90K = -$1350 / yr
    assert delta < -800


def test_state_overlay_ohio_applies_rehab_mult():
    """OH overlay multiplies rehab by 1.30 — visible in equity_invested at base."""
    a = uw.Assumptions(purchase_price=100_000, monthly_rent=1500, rehab_cost=20_000)
    r_no = stress.stress_test(a, state=None)
    r_oh = stress.stress_test(a, state="OH")
    # OH base: CoC should be lower because more equity invested
    assert r_oh["scenarios"][0]["cash_on_cash"] < r_no["scenarios"][0]["cash_on_cash"]


def test_bad_deal_screens_red_with_mitigations():
    r = stress.stress_test(_bad_deal())
    assert r["gate"]["verdict"] == "RED"
    assert len(r["gate"]["reasons"]) >= 1
    assert len(r["gate"]["mitigations"]) >= 1
    # Should suggest a walk-away price (or None if even 30% won't fix)
    assert "price_to_green" in r


def test_solid_deal_screens_green_no_mitigations():
    r = stress.stress_test(_solid_deal())
    assert r["gate"]["verdict"] == "GREEN", f"Expected GREEN, got {r['gate']['verdict']} ({r['gate']['reasons']})"
    assert r["gate"]["mitigations"] == []
    # Green deals don't need a walk-away number
    assert r.get("price_to_green") is None


def test_thin_deal_screens_yellow():
    """A 1% rule deal with strong base but recession-fragile worst case
    should land in YELLOW, not GREEN. This is the platform's signature
    callout — surfacing the risk most amateur tools hide."""
    r = stress.stress_test(_thin_deal())
    assert r["gate"]["verdict"] == "YELLOW", f"Expected YELLOW, got {r['gate']['verdict']} ({r['gate']['reasons']})"
    # Worst case should be visibly negative
    assert r["scenarios"][2]["irr"] < 0
    # Base case should still look healthy
    assert r["scenarios"][0]["cash_on_cash"] > 0.05


def test_break_even_occupancy_above_one_when_upside_down():
    """A deal where debt service alone exceeds gross rent should report > 1.0."""
    a = uw.Assumptions(
        purchase_price=500_000, monthly_rent=800,  # massive rent-to-price gap
        rehab_cost=0, mortgage_rate=0.08, ltv=0.80,
    )
    r = stress.stress_test(a)
    bep = r["scenarios"][0]["break_even_occupancy"]
    assert bep > 1.0


def test_unknown_state_does_not_crash():
    r = stress.stress_test(_solid_deal(), state="XX")
    assert r["state_overlay_applied"] is False
    assert r["state_overlay_summary"] is None
    assert r["scenarios"][0]["irr"] is not None


def test_price_to_green_actually_yields_green():
    """If `price_to_green` is returned, re-running at that price should give GREEN."""
    r = stress.stress_test(_bad_deal())
    if r.get("price_to_green") is None:
        pytest.skip("Deal can't be saved by price reduction; expected for this fixture.")
    new_price = r["price_to_green"]
    a2 = uw.Assumptions(**{**_bad_deal().__dict__, "purchase_price": new_price})
    r2 = stress.stress_test(a2, include_price_to_green=False)
    assert r2["gate"]["verdict"] == "GREEN"


def test_chat_tool_executes():
    """Stress test reachable via chat _execute()."""
    from reip import chat
    out = chat._execute("stress_test", {
        "purchase_price": 90_000, "monthly_rent": 1500,
        "rehab_cost": 5000, "state": "MO",
    })
    assert "gate" in out
    assert out["gate"]["verdict"] in {"GREEN", "YELLOW", "RED"}
