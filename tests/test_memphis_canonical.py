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
# Documented gaps — these tests skip until the BRRRR + mitigations work
# lands. They're here as executable open-threads, not assertions.
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason=(
    "BRRRR refi mechanics not yet wired. Add packages/underwriting/brrrr.py "
    "per build spec Phase 3; this test then pins $7,250 residual / 13% CoC "
    "within $50."
))
def test_memphis_brrrr_base_case_matches_long_paper_table_9(memphis: dict):
    pass


@pytest.mark.skip(reason=(
    "Mitigations parameter not yet on reip's gate. Add `mitigations=` to "
    "stress_test signature per build spec §4 REQUIRED_MITIGATIONS_BY_FAILURE; "
    "this test then pins RED → YELLOW upgrade with all five verified."
))
def test_memphis_upgrades_to_yellow_with_all_mitigations_verified(memphis: dict):
    pass
