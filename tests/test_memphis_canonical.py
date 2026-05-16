"""Memphis Sample Deal — canonical correctness fixture.

This test is the platform's moral center: every code change must screen
the Memphis Sample Deal RED on default rules. If this test fails, the
recommendation gate has drifted.

Source: RealEstate_Investment_Framework_v5.docx Section 9 +
        PLATFORM_BUILD_SPEC.md §4 Recommendation gate.

Fixture: tests/fixtures/memphis_canonical.yaml

What's pinned today:
  - Deal screens RED on default rules (no mitigations).
  - Base DSCR < 1.30 (below GREEN threshold).
  - Base CoC < 6% (below GREEN threshold).

What's NOT pinned today (Phase 3 follow-ups):
  - "RED → YELLOW with all 5 mitigations verified" — reip's current
    gate has no `mitigations` parameter. Add it per build spec §4
    `REQUIRED_MITIGATIONS_BY_FAILURE`.
  - "$7,250 residual / 13% CoC base case" within $50 — reip's current
    stress_test models purchase financing as buy-and-hold; the BRRRR
    refi mechanics in Section 9.2 need `packages/underwriting/brrrr.py`
    per the build spec. The fixture documents the target numbers; the
    BRRRR module + assertions land in a follow-up commit.
  - Sensitivity scenario residual capital + CoC — same dependency.
"""
from __future__ import annotations
from pathlib import Path
import yaml
import pytest

from reip import stress, underwriting as uw, climate
from reip.store import connect


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memphis_canonical.yaml"


@pytest.fixture(scope="module")
def memphis() -> dict:
    """Load the canonical Memphis Sample Deal fixture."""
    with open(FIXTURE_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def memphis_stress_result(memphis: dict) -> dict:
    """Run reip's stress_test against the fixture once, share across tests."""
    inputs = memphis["stress_test_inputs"]
    a = uw.Assumptions(
        purchase_price=inputs["purchase_price"],
        rehab_cost=inputs["rehab_cost"],
        arv=inputs["arv"],
        monthly_rent=inputs["monthly_rent"],
        mortgage_rate=inputs["mortgage_rate"],
        ltv=inputs["ltv"],
        vacancy=inputs["vacancy"],
        property_tax_rate=inputs["property_tax_rate"],
        insurance_annual=inputs["insurance_annual"],
        hoa_monthly=inputs["hoa_monthly"],
    )
    con = connect()
    cs = climate.score_zip(con, inputs["zip"], inputs["state"])
    climate_dict = climate.to_dict(cs) if cs else None
    return stress.stress_test(a, state=inputs["state"], climate_score=climate_dict)


def _scenario(result: dict, name: str) -> dict:
    """Pull a scenario by name from the stress_test output."""
    for s in result["scenarios"]:
        if s["name"] == name:
            return s
    raise KeyError(f"scenario {name!r} not in result")


# ---------------------------------------------------------------------------
# Pinned: default rules → RED
# ---------------------------------------------------------------------------

def test_memphis_screens_red_on_default(memphis: dict, memphis_stress_result: dict):
    """The headline assertion. If 1.07x DSCR is the threshold then this
    deal must screen RED, even by a hair, or the gate erodes into a
    soft suggestion (long paper Section 9.4)."""
    expected = memphis["expected"]["default_verdict"]
    actual = memphis_stress_result["gate"]["verdict"]
    assert actual == expected, (
        f"Memphis Sample Deal must screen {expected} on default rules — "
        f"got {actual}. Either the deal inputs drifted or the gate softened. "
        f"Reasons returned: {memphis_stress_result['gate'].get('reasons')}"
    )


def test_memphis_base_dscr_below_green_threshold(memphis: dict, memphis_stress_result: dict):
    """Defense-in-depth: the gate's DSCR check is the principal reason
    this deal screens RED in the long paper. Pin the base DSCR shape so
    a future refactor can't accidentally inflate it past the GREEN line."""
    cap = memphis["expected"]["default_dscr_below"]
    base = _scenario(memphis_stress_result, "base")
    assert base["dscr"] < cap, (
        f"Memphis base DSCR must be < {cap} (GREEN-threshold floor); "
        f"got {base['dscr']}"
    )


def test_memphis_base_coc_below_green_threshold(memphis: dict, memphis_stress_result: dict):
    """The other GREEN-threshold dimension. Memphis must miss this too."""
    cap = memphis["expected"]["default_base_coc_below"]
    base = _scenario(memphis_stress_result, "base")
    assert base["cash_on_cash"] < cap, (
        f"Memphis base CoC must be < {cap} (GREEN-threshold floor); "
        f"got {base['cash_on_cash']}"
    )


def test_memphis_fixture_has_all_five_mitigation_groups(memphis: dict):
    """The fixture must enumerate the five mitigations from §9.4.
    The Phase 3 gate will consume these once it grows a mitigations
    parameter; until then, pin the inventory."""
    mits = memphis["mitigations"]
    upgrade_groups = set()
    for m in mits:
        upgrade_groups.update(m["upgrades"])
    # The build spec REQUIRED_MITIGATIONS_BY_FAILURE has four groups.
    expected_groups = {"thin_dscr", "rehab_overrun_risk", "financing_risk", "exit_risk"}
    assert upgrade_groups == expected_groups, (
        f"Memphis fixture must cover the four mitigation groups from "
        f"build spec §4. Missing: {expected_groups - upgrade_groups}; "
        f"Unexpected: {upgrade_groups - expected_groups}"
    )


# ---------------------------------------------------------------------------
# Activated 2026-05-16 — Phase 3 work landed (brrrr.py + mitigations gate).
# These were @pytest.mark.skip until the BRRRR module + mitigations
# parameter shipped. Now they pin the v5 §9.2 base-case numbers and the
# §9.4 RED→YELLOW upgrade.
# ---------------------------------------------------------------------------

def test_memphis_brrrr_base_case_matches_long_paper_table_9(memphis: dict):
    """v5 §9.2 Table 9 walkthrough. Acceptance per build spec §3 Phase 3:
    'Memphis fixture matches long paper Table 9 base-case numbers within $50.'"""
    from reip.brrrr import BRRRRInputs, compute_brrrr
    deal = memphis["deal"]
    i = BRRRRInputs(
        purchase_price=deal["purchase_price"],
        rehab_cost=deal["rehab_cost"],
        arv=deal["arv"],
        monthly_rent=deal["monthly_rent"],
        annual_opex=deal["annual_opex"],
        holding_cost=deal["holding_cost"],
        closing_cost_pct=deal["closing_cost_pct"],
        refi_closing_cost=deal["refi_closing_costs"],
        refi_ltv=deal["refi_ltv"],
        refi_rate=deal["refi_rate"],
        refi_term_years=deal["refi_term_years"],
    )
    out = compute_brrrr(i)
    # All checks within $50 (build-spec acceptance tolerance).
    assert abs(out.total_invested_before_refi - deal["total_invested_before_refi"]) <= 50, (
        f"total_invested_before_refi {out.total_invested_before_refi} "
        f"missed Table 9 ${deal['total_invested_before_refi']} by >$50"
    )
    assert abs(out.stabilized_noi - deal["stabilized_noi"]) <= 50
    assert abs(out.refi_proceeds_net - deal["refi_proceeds_net"]) <= 50
    assert abs(out.residual_capital - deal["residual_capital"]) <= 50
    assert abs(out.annual_cashflow_after_debt_service - deal["annual_cashflow_after_debt_service"]) <= 50
    # DSCR & CoC are unitless — pin to 2 decimal places.
    assert abs(out.stabilized_dscr - deal["stabilized_dscr"]) <= 0.01, (
        f"stabilized DSCR {out.stabilized_dscr:.3f} missed v5's 1.07x"
    )
    assert out.cash_on_cash_on_residual is not None
    assert abs(out.cash_on_cash_on_residual - deal["cash_on_cash_on_residual"]) <= 0.01


def test_memphis_upgrades_to_yellow_with_all_mitigations_verified(memphis: dict):
    """v5 §9.4: 'Only when all five mitigations are verified does the RED
    screen upgrade to YELLOW.' Pin that contract.

    Five mitigations enumerated in the fixture. Pass all of them in →
    expect YELLOW with via_mitigations=True. Pass a subset → still RED.
    """
    inputs = memphis["stress_test_inputs"]
    a = uw.Assumptions(
        purchase_price=inputs["purchase_price"],
        rehab_cost=inputs["rehab_cost"],
        arv=inputs["arv"],
        monthly_rent=inputs["monthly_rent"],
        mortgage_rate=inputs["mortgage_rate"],
        ltv=inputs["ltv"],
        vacancy=inputs["vacancy"],
        property_tax_rate=inputs["property_tax_rate"],
        insurance_annual=inputs["insurance_annual"],
        hoa_monthly=inputs["hoa_monthly"],
    )
    all_mits = [m["id"] for m in memphis["mitigations"]]
    assert len(all_mits) == 6, "fixture should enumerate 6 mitigation IDs"

    # All verified → RED upgrades to YELLOW
    out = stress.stress_test(a, state=inputs["state"], verified_mitigations=all_mits)
    assert out["gate"]["verdict"] == "YELLOW", (
        f"With all mitigations verified, Memphis should upgrade RED→YELLOW; "
        f"got {out['gate']['verdict']}. Reasons: {out['gate']['reasons']}"
    )
    assert out["gate"]["via_mitigations"] is True
    assert memphis["expected"]["with_all_mitigations_verdict"] == "YELLOW"

    # Subset (missing the LTR fallback PM) → still RED. exit_risk failure
    # code is in the gate output but its mitigation is unverified, so
    # upgrade is denied.
    partial = [m for m in all_mits if m != "ltr_fallback_pm_identified"]
    out_partial = stress.stress_test(a, state=inputs["state"], verified_mitigations=partial)
    assert out_partial["gate"]["verdict"] == "RED", (
        f"With LTR-fallback missing, Memphis should stay RED; "
        f"got {out_partial['gate']['verdict']}. "
        f"Failure codes were: {out_partial['gate']['failure_codes']}"
    )
    assert out_partial["gate"]["via_mitigations"] is False


def test_memphis_brrrr_fixture_sensitivity_scenarios_present(memphis: dict):
    """v5 §9.3 Table 10. Pin that the fixture carries all six sensitivity
    scenarios (base + 5 stresses) so a later module can run them through
    a sensitivity engine without re-deriving the inputs."""
    names = {s["name"] for s in memphis["sensitivity"]}
    assert names == {
        "base", "rehab_over_20pct", "arv_miss_5pct",
        "rehab_arv_combined", "catastrophic", "bull_arv_up_5pct",
    }
