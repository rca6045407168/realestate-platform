"""Portfolio + tax-adjusted IRR tests.

The tax math is the place where it's easiest to get a sign wrong (or
double-count a deduction). These tests pin down the contract.
"""
from __future__ import annotations
import pytest
from reip import portfolio, tax


def _deal(label, price, rent, state, verdict, base_cf, base_irr,
          worst_irr=-0.15, dscr=1.25, rehab=0, status="underwritten"):
    """Tiny test factory."""
    return {
        "label": label, "status": status,
        "inputs": {
            "purchase_price": price, "monthly_rent": rent, "rehab_cost": rehab,
            "ltv": 0.75, "mortgage_rate": 0.07, "state": state,
        },
        "stress": {
            "gate": {"verdict": verdict},
            "scenarios": [
                {"irr": base_irr, "cash_flow_y1": base_cf, "dscr": dscr, "cash_on_cash": 0.05},
                {"irr": base_irr - 0.10, "cash_flow_y1": base_cf - 800, "dscr": dscr - 0.20, "cash_on_cash": 0.02},
                {"irr": worst_irr, "cash_flow_y1": base_cf - 2000, "dscr": dscr - 0.40, "cash_on_cash": -0.02},
            ],
        },
    }


# ---- tax.py ---------------------------------------------------------------

def test_depreciation_excludes_land():
    """A $100K house with 20% land alloc and 27.5y life should depreciate $2909/yr."""
    d = tax.annual_depreciation(100_000, rehab_cost=0, land_allocation=0.20)
    assert round(d) == round(80_000 / 27.5)


def test_depreciation_includes_rehab():
    """Rehab cost adds to depreciable basis."""
    d0 = tax.annual_depreciation(100_000, rehab_cost=0)
    d1 = tax.annual_depreciation(100_000, rehab_cost=20_000)
    assert d1 > d0
    # Rehab adds (20k × 0.80 / 27.5) ≈ $582
    assert round(d1 - d0) == round(20_000 * 0.80 / 27.5)


def test_tax_savings_always_non_negative():
    """`annual_tax_savings` is the depreciation alpha — never negative."""
    # Positive CF, partial shield
    s1 = tax.annual_tax_savings(pretax_cf=3700, depreciation=2473)
    assert s1 > 0
    # Negative CF, active deduction → big savings (offset against ordinary income)
    s2 = tax.annual_tax_savings(pretax_cf=-9200, depreciation=9018)
    assert s2 > 0
    assert s2 > s1
    # Zero everything
    s3 = tax.annual_tax_savings(pretax_cf=0, depreciation=0)
    assert s3 == 0
    # Passive case: no current-year savings on loss
    t_passive = tax.TaxAssumptions(deduction_against_ordinary=False)
    s4 = tax.annual_tax_savings(pretax_cf=-9200, depreciation=9018, tax=t_passive)
    # Passive baseline pays $0 tax on loss; depreciation can shield to zero only
    # → no savings vs baseline. So 0.
    assert s4 == 0


def test_post_tax_cf_partial_shield():
    """Cash-positive deal where depreciation only partially shields."""
    p = tax.post_tax_annual_cf(3700, 2473, tax.TaxAssumptions(tax_bracket=0.32))
    # taxable = $3700 - $2473 = $1227 → tax = $393 → post = $3307
    assert 3300 <= p <= 3315


def test_post_tax_cf_paper_loss_active():
    """Cash-negative deal: active deduction offsets W-2 → post-tax CF rises."""
    p = tax.post_tax_annual_cf(-9200, 9018, tax.TaxAssumptions(tax_bracket=0.32))
    # taxable = -$9200 - $9018 = -$18218 × 32% = -$5830 (refund)
    # post = -$9200 - (-$5830) = -$3370
    assert -3380 <= p <= -3360


def test_post_tax_cf_passive_loss_suspended():
    """Passive: depreciation can shield CF to zero only; excess loss suspends."""
    p = tax.post_tax_annual_cf(-9200, 9018,
                                tax.TaxAssumptions(deduction_against_ordinary=False, tax_bracket=0.32))
    # depreciation < |negative CF| so it shields the negative CF down to negative
    # actually: taxable = max(0, -9200 - 9018) = 0, tax = 0, post = -9200
    # i.e. depreciation doesn't help when CF is already negative in passive case
    assert p == -9200


# ---- portfolio.py ---------------------------------------------------------

def test_empty_portfolio_returns_empty_view():
    out = portfolio.aggregate([])
    assert out["count"] == 0
    assert out["totals"]["equity_deployed"] == 0
    assert out["by_state"] == []
    assert out["concentration_warnings"] == []


def test_single_deal_aggregation():
    out = portfolio.aggregate([_deal("Test", 80_000, 1700, "MO", "GREEN", 3700, 0.42)])
    assert out["count"] == 1
    # Equity = price × (1-LTV) + price × 0.03 closing = $20,000 + $2,400 = $22,400
    assert 22_000 < out["totals"]["equity_deployed"] < 23_000
    assert out["by_state"][0]["key"] == "MO"
    assert out["by_state"][0]["pct"] == 1.0


def test_concentration_warning_single_state():
    """3 deals all in FL → warns single-state concentration."""
    deals = [
        _deal("d1", 200_000, 2000, "FL", "GREEN", 500, 0.10),
        _deal("d2", 200_000, 2000, "FL", "GREEN", 500, 0.10),
        _deal("d3", 200_000, 2000, "FL", "GREEN", 500, 0.10),
    ]
    out = portfolio.aggregate(deals)
    text = " ".join(out["concentration_warnings"])
    assert "FL" in text


def test_concentration_warning_climate_states():
    """Mix of FL + TX should still trigger climate warning even if no single state >60%."""
    deals = [
        _deal("FL deal", 250_000, 2000, "FL", "GREEN", 500, 0.10),
        _deal("TX deal", 250_000, 2000, "TX", "GREEN", 500, 0.10),
        _deal("OH deal", 100_000, 1500, "OH", "GREEN", 800, 0.20),
    ]
    out = portfolio.aggregate(deals)
    text = " ".join(out["concentration_warnings"]).lower()
    assert "climate" in text or "fl" in text


def test_red_deal_concentration_warning():
    """>=20% equity on RED-verdict deals → warning."""
    deals = [
        _deal("GREEN d", 80_000, 1700, "MO", "GREEN", 3700, 0.42),
        _deal("RED d", 300_000, 2800, "FL", "RED", -9000, -0.99),
    ]
    out = portfolio.aggregate(deals)
    text = " ".join(out["concentration_warnings"]).lower()
    assert "red" in text


def test_weighted_irr_respects_equity():
    """Larger equity should pull the weighted IRR toward its deal's IRR."""
    deals = [
        _deal("small GREEN", 50_000, 1500, "MO", "GREEN", 2000, 0.40),
        _deal("big RED",     500_000, 2500, "FL", "RED", -8000, -0.20),
    ]
    out = portfolio.aggregate(deals)
    # big deal is 10x the equity, so weighted IRR should be much closer to its IRR
    w = out["totals"]["weighted_irr_pretax"]
    assert w < 0  # the big negative dominates


def test_post_tax_lifts_irr_when_depreciation_dominates():
    """A deal with sub-depreciation cash flow should have post-tax IRR > pre-tax."""
    # Big depreciation, modest CF → active deduction creates strong shield
    deals = [_deal("big home", 400_000, 3000, "TX", "YELLOW", -2000, 0.05, dscr=0.95, rehab=0)]
    out = portfolio.aggregate(deals, tax=tax.TaxAssumptions(tax_bracket=0.32))
    d = out["deals_with_tax"][0]
    assert d["irr_posttax"] > d["irr_pretax"]


def test_tax_assumptions_round_trip():
    out = portfolio.aggregate([_deal("d", 100_000, 1500, "MO", "GREEN", 500, 0.10)],
                              tax=tax.TaxAssumptions(tax_bracket=0.24, land_allocation=0.25,
                                                     deduction_against_ordinary=False))
    a = out["tax_assumptions"]
    assert a["tax_bracket"] == 0.24
    assert a["land_allocation"] == 0.25
    assert a["deduction_against_ordinary"] is False


def test_bucket_totals_match_overall():
    """Sum of equity across by_state buckets = total equity_deployed."""
    deals = [
        _deal("d1", 80_000, 1700, "MO", "GREEN", 3700, 0.42),
        _deal("d2", 120_000, 1600, "OH", "YELLOW", 200, 0.08),
        _deal("d3", 300_000, 2500, "FL", "RED", -9000, -0.20),
    ]
    out = portfolio.aggregate(deals)
    state_sum = sum(s["equity"] for s in out["by_state"])
    assert round(state_sum, 2) == round(out["totals"]["equity_deployed"], 2)
