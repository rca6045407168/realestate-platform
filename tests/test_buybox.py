"""Buy-box derivation + ARV estimator tests.

Tests the two functions that translate zip-level macro data into
property-level targets an investor can act on:

  - buybox.derive(): full BuyBox object with price/rent/rehab/ARV bands,
    a typical_deal blob for direct /api/stress feeding, and contextual notes.
  - buybox.arv_estimate(): multi-horizon trend-based ARV with caveats.
"""
from __future__ import annotations
import pytest

from reip.store import connect
from reip import buybox


# Use a known-good zip we've verified shows up in the data.
KCMO_ZIP = "64120"
MEMPHIS_ZIP = "38109"
MIAMI_ZIP = "33186"


def _con():
    return connect()


def test_buybox_derive_basic_shape():
    b = buybox.derive(_con(), KCMO_ZIP)
    assert b is not None, "KCMO zip should exist in ZHVI data"
    assert b.zip == KCMO_ZIP
    assert b.state == "MO", f"State should normalize to 2-letter code, got {b.state!r}"
    # Price band monotonic and centered on ZHVI
    assert b.target_price_low < b.target_price_mid < b.target_price_high
    # Rent band monotonic
    assert b.target_rent_low < b.target_rent_mid < b.target_rent_high
    # Rehab band sane
    assert 0 < b.target_rehab_light < b.target_rehab_heavy
    # ARV present
    assert b.arv_now > 0
    assert b.arv_trend_12mo > 0
    # Method documented
    assert "trend-based" in b.arv_method.lower()


def test_buybox_typical_deal_feeds_stress():
    """The typical_deal field must be directly pluggable into stress.stress_test."""
    from reip import stress, underwriting as uw
    b = buybox.derive(_con(), KCMO_ZIP)
    td = b.typical_deal
    a = uw.Assumptions(
        purchase_price=td["purchase_price"],
        monthly_rent=td["monthly_rent"],
        rehab_cost=td["rehab_cost"],
        mortgage_rate=td["mortgage_rate"],
        ltv=td["ltv"],
        vacancy=td["vacancy"],
        insurance_annual=td["insurance_annual"],
        property_tax_rate=td["property_tax_rate"],
    )
    r = stress.stress_test(a, state=td["state"])
    assert r["gate"]["verdict"] in {"GREEN", "YELLOW", "RED"}
    # state should have been honored by the overlay
    if td["state"] == "MO":
        assert r["state"] == "MO"
        assert r["state_overlay_applied"] is True


def test_buybox_regime_label_matches_growth():
    """Florida should be flagged contracting; KCMO should be expanding."""
    b_kcmo = buybox.derive(_con(), KCMO_ZIP)
    b_miami = buybox.derive(_con(), MIAMI_ZIP)
    assert b_kcmo.regime_label in {"expanding", "mixed"}
    # Miami should be at most mixed (recent FL softening)
    assert b_miami.regime_label in {"mixed", "contracting", "crash"}


def test_buybox_contracting_regime_warns():
    b = buybox.derive(_con(), MEMPHIS_ZIP)
    if b.regime_label in {"contracting", "crash"}:
        # Should surface the "wait or lowball" note
        text = " ".join(b.notes).lower()
        assert "lowball" in text or "wait" in text or "weighing" in text or "weigh" in text


def test_buybox_unknown_zip_returns_none():
    b = buybox.derive(_con(), "00000")
    assert b is None


def test_buybox_state_normalization():
    """Whether the DB returns 'Missouri' or 'MO', the buy box yields 'MO'."""
    assert buybox._to_state_code("Missouri") == "MO"
    assert buybox._to_state_code("MO") == "MO"
    assert buybox._to_state_code("mo") == "MO"
    assert buybox._to_state_code(None) is None
    assert buybox._to_state_code("Atlantis") is None


# ---- ARV estimator ---------------------------------------------------------

def test_arv_estimate_horizons_grow_when_market_is_hot():
    """In an expanding market the longer horizon should be ≥ today's ARV."""
    out = buybox.arv_estimate(_con(), KCMO_ZIP)
    assert out is not None
    horizons = {h["years"]: h["projected_arv"] for h in out["horizons"]}
    if out["decayed_growth_pa"] > 0:
        assert horizons[2.0] >= horizons[1.0] >= horizons[0.5] >= horizons[0.0]


def test_arv_estimate_caveats_present():
    out = buybox.arv_estimate(_con(), KCMO_ZIP)
    assert len(out["caveats"]) >= 2
    # Must call out trend-based limitation honestly
    text = " ".join(out["caveats"]).lower()
    assert "trend" in text
    assert "comp" in text   # mentions comp-based as the better path


def test_arv_estimate_unknown_zip_returns_none():
    assert buybox.arv_estimate(_con(), "00000") is None


# ---- Chat tool wiring ------------------------------------------------------

def test_buy_box_chat_tool_executes():
    from reip import chat
    out = chat._execute("buy_box", {"zip": KCMO_ZIP})
    assert "typical_deal" in out
    assert out["typical_deal"]["state"] == "MO"


def test_chat_pipeline_block_renders():
    """_format_pipeline_block should produce a non-empty system-prompt block."""
    from reip import chat
    block = chat._format_pipeline_block([
        {"label": "Test deal", "status": "underwritten", "verdict": "GREEN",
         "purchase_price": 80000, "monthly_rent": 1700, "state": "MO",
         "base_irr": 0.42, "worst_irr": 0.11, "price_to_green": None,
         "notes": "called agent"},
    ])
    assert "Test deal" in block
    assert "GREEN" in block
    assert "$80,000" in block
    assert "MO" in block
    assert "called agent" in block


def test_chat_pipeline_block_empty_returns_empty():
    from reip import chat
    assert chat._format_pipeline_block([]) == ""
    assert chat._format_pipeline_block(None) == ""
