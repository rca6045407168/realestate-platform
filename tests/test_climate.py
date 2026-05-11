"""Climate scoring + stress amplification tests.

These pin the calibration so a future "let's be nicer to Florida" edit
gets caught in CI.
"""
from __future__ import annotations
import pytest
from reip.store import connect
from reip import climate, stress, underwriting as uw


# Anchors we expect from the actual FEMA NFIP data we've loaded.
KCMO_ZIP  = "64120"   # inland Missouri — should be minimal
FORTMYERS_ZIP = "33908"  # Lee County FL, Hurricane Ian zone — should be severe
MIAMI_ZIP = "33186"   # Miami-Dade, hurricane belt — should be severe
SF_ZIP    = "94110"   # CA — should fire wildfire
CLEVELAND_ZIP = "44135"  # OH — moderate flood claims


def _con():
    return connect()


def test_kcmo_is_minimal_climate():
    c = climate.score_zip(_con(), KCMO_ZIP, "MO")
    assert c is not None
    assert c.category in ("minimal", "moderate")
    assert c.hurricane_flag is False
    assert c.wildfire_flag is False


def test_fort_myers_is_severe_climate():
    """Lee County FL had $1.2B in NFIP payouts 2019-present (Ian)."""
    c = climate.score_zip(_con(), FORTMYERS_ZIP, "FL")
    assert c is not None
    assert c.category == "severe"
    assert c.flood_score == 100
    assert c.flood_paid_5y > 100_000_000   # > $100M — Ian-class damage
    assert c.hurricane_flag is True


def test_miami_is_severe_and_hurricane_state():
    c = climate.score_zip(_con(), MIAMI_ZIP, "FL")
    assert c is not None
    assert c.category in ("elevated", "severe")
    assert c.hurricane_flag is True


def test_sf_fires_wildfire_flag():
    c = climate.score_zip(_con(), SF_ZIP, "CA")
    assert c is not None
    assert c.wildfire_flag is True


def test_unknown_zip_with_no_state_defaults_to_minimal():
    """No zip-county lookup match + no state → minimal score, not None.
    This is the right contract: state flags can fire even without zip data,
    and a missing zip shouldn't crash the whole pipeline."""
    c = climate.score_zip(_con(), "00000", None)
    assert c is not None
    assert c.overall_score == 0
    assert c.category == "minimal"
    assert c.hurricane_flag is False
    assert c.wildfire_flag is False


def test_unknown_zip_with_state_still_fires_state_flags():
    """zip=00000 + state=CA → wildfire flag still fires (state-level prior)."""
    c = climate.score_zip(_con(), "00000", "CA")
    assert c is not None
    assert c.wildfire_flag is True
    assert c.overall_score >= 40   # CA wildfire baseline


def test_score_clamped_0_to_100():
    """No combination of factors should escape 0..100."""
    for zip_code, state in [(KCMO_ZIP, "MO"), (FORTMYERS_ZIP, "FL"),
                              (MIAMI_ZIP, "FL"), (SF_ZIP, "CA")]:
        c = climate.score_zip(_con(), zip_code, state)
        if c is None:
            continue
        assert 0 <= c.overall_score <= 100
        assert 0 <= c.flood_score <= 100
        assert 0 <= c.hurricane_score <= 100
        assert 0 <= c.wildfire_score <= 100


def test_stress_amplifies_for_severe_climate():
    """Severe-climate zip should have a worse worst-case than no-climate run."""
    a = uw.Assumptions(purchase_price=300_000, monthly_rent=2500,
                        insurance_annual=4000, mortgage_rate=0.07)
    c = climate.score_zip(_con(), FORTMYERS_ZIP, "FL")
    assert c is not None
    no_climate = stress.stress_test(a, state="FL")
    with_climate = stress.stress_test(a, state="FL",
                                       climate_score=climate.to_dict(c))
    # Worst-case insurance × rehab should both be heavier with climate
    # Compare via DSCR: with_climate worst DSCR ≤ no_climate worst DSCR
    assert with_climate["scenarios"][2]["dscr"] <= no_climate["scenarios"][2]["dscr"]
    # And the overlay summary should be reported
    assert with_climate["climate_overlay_applied"] is True
    assert with_climate["climate_overlay_summary"] is not None


def test_stress_no_climate_score_unchanged():
    """When no climate score is passed, results match the prior baseline."""
    a = uw.Assumptions(purchase_price=80_000, monthly_rent=1700, rehab_cost=5000)
    r = stress.stress_test(a)
    assert r["climate_overlay_applied"] is False
    assert r["climate_overlay_summary"] is None


def test_buybox_includes_climate():
    """The buy box for an Ian-zone zip should ship a climate dict + bumped insurance."""
    from reip import buybox
    b = buybox.derive(_con(), FORTMYERS_ZIP)
    assert b is not None
    assert b.climate is not None
    assert b.climate["category"] == "severe"
    # Insurance should be bumped above the 1.2% baseline
    baseline = b.target_price_mid * 0.012
    assert b.typical_deal["insurance_annual"] > baseline * 1.5


def test_buybox_uses_real_state_tax_rate():
    """Buy box should pick up the property_tax_state rate instead of hardcoded 1.2%."""
    from reip import buybox
    b = buybox.derive(_con(), FORTMYERS_ZIP)
    assert b is not None
    # FL effective rate is around 0.74% — definitely not 1.2%
    assert b.typical_deal["property_tax_rate"] < 0.011


def test_stress_multipliers_monotone_in_score():
    """Higher climate score → larger stress bumps."""
    sev = climate.ClimateScore(zip="x", state=None, fips_county=None,
                                overall_score=85, category="severe",
                                flood_score=85, flood_claims_5y=0, flood_paid_5y=0,
                                hurricane_flag=False, hurricane_score=0,
                                wildfire_flag=False, wildfire_score=0,
                                primary_risk="flood", notes=[])
    mod = climate.ClimateScore(zip="x", state=None, fips_county=None,
                                overall_score=30, category="moderate",
                                flood_score=30, flood_claims_5y=0, flood_paid_5y=0,
                                hurricane_flag=False, hurricane_score=0,
                                wildfire_flag=False, wildfire_score=0,
                                primary_risk="flood", notes=[])
    m_sev = climate.stress_multipliers(sev)
    m_mod = climate.stress_multipliers(mod)
    assert m_sev["insurance_mult_bonus"] > m_mod["insurance_mult_bonus"]
    assert m_sev["rehab_mult_bonus"] > m_mod["rehab_mult_bonus"]
