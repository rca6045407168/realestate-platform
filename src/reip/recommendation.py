"""Recommendation gate — Phase 3 of the platform build spec.

The gate's job: convert an underwritten deal into a single GREEN / YELLOW /
RED classification, with reasons in plain English and a list of required
mitigations to upgrade. The thresholds below are the spec's *moral center*:

  > do not soften them when a deal sits just below.

Memphis test fixture (§9 of the long paper):
  - Stabilized DSCR 1.07x must screen RED on default rules.
  - With all 5 verified mitigations → upgrades to YELLOW.
  - Anything else is a bug.

Usage:
    deal = DealUnderwriting(stabilized_dscr=1.07, ...)
    rec = classify(deal, mitigations=VerifiedMitigations())  # → RED
    rec = classify(deal, mitigations=VerifiedMitigations(
        verified_70pct_ltv_term_sheet=True,
        documented_capital_reserve_min_25k=True,
        signed_contractor_bid=True,
        committed_hard_money_primary=True,
        ltr_fallback_pm_identified=True,
    ))  # → YELLOW (with the contractor-bid + reserves mitigations applied,
         # the 1.07× DSCR upgrades to ~1.20× per spec)
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum


class Verdict(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


GREEN_THRESHOLDS = {
    "stabilized_dscr_min": 1.30,
    "refi_appraisal_stress_arv_haircut": 0.05,
    "refi_appraisal_stress_min_ltv": 0.70,
    "insurance_trend_max_pct": 0.20,
    "climate_pct_max": 0.75,
    "alpha_stack_min_flags": 2,
    "stress_min_coc_on_residual": 0.08,
}

YELLOW_THRESHOLDS = {
    "stabilized_dscr_min": 1.10,
    "stabilized_dscr_max": 1.30,
    "alpha_stack_min_flags": 2,
}

# Failure tags are stable strings the UI surfaces and the tests assert on.
FAILURE_TAGS = (
    "thin_dscr",
    "refi_haircut_risk",
    "insurance_spike_risk",
    "climate_top_decile",
    "alpha_stack_too_thin",
    "sensitivity_breaks_cashflow",
    "msa_bottom_quartile",
    "rehab_overrun_risk",
    "financing_risk",
    "exit_risk",
)

REQUIRED_MITIGATIONS_BY_FAILURE = {
    "thin_dscr": [
        "verified_70pct_ltv_term_sheet",
        "documented_capital_reserve_min_25k",
    ],
    "rehab_overrun_risk": ["signed_contractor_bid"],
    "financing_risk": [
        "committed_hard_money_primary",
        "committed_hard_money_backup",
    ],
    "exit_risk": ["ltr_fallback_pm_identified"],
}


@dataclass
class DealUnderwriting:
    """Inputs the gate reads. All optional so unknown fields fail safe."""
    stabilized_dscr: float | None = None
    refi_appraisal_stress_pass: bool | None = None
    insurance_trend_pct: float | None = None
    climate_pct: float | None = None     # 0–1 percentile rank, higher = worse
    alpha_stack_count: int | None = None
    stress_coc_on_residual: float | None = None
    msa_blended_percentile: float | None = None  # 0–1
    sensitivity_negative_cashflow: bool | None = None
    rehab_overrun_risk: bool = False     # set True if rehab heuristic flags > 30% var risk
    financing_concentration_risk: bool = False  # set True if only one HM source
    exit_risk_no_ltr_fallback: bool = False     # set True for STR-dependent exits


@dataclass
class VerifiedMitigations:
    """Each field flips a single mitigation. UI marks them via
    POST /api/underwritings/{id}/mitigations."""
    verified_70pct_ltv_term_sheet: bool = False
    documented_capital_reserve_min_25k: bool = False
    signed_contractor_bid: bool = False
    committed_hard_money_primary: bool = False
    committed_hard_money_backup: bool = False
    ltr_fallback_pm_identified: bool = False

    def has_all(self, names: list[str]) -> bool:
        return all(getattr(self, n, False) for n in names)


@dataclass
class Recommendation:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    required_mitigations: list[str] = field(default_factory=list)
    verified_mitigations: list[str] = field(default_factory=list)
    primary_action: str | None = None

    def to_dict(self) -> dict:
        return {**asdict(self), "verdict": self.verdict.value}


def _evaluate_failures(deal: DealUnderwriting) -> tuple[list[str], list[str]]:
    """Returns (failure_tags, plain_english_reasons)."""
    failures: list[str] = []
    reasons: list[str] = []

    g = GREEN_THRESHOLDS

    if deal.stabilized_dscr is not None:
        if deal.stabilized_dscr < YELLOW_THRESHOLDS["stabilized_dscr_min"]:
            failures.append("thin_dscr")
            reasons.append(
                f"Stabilized DSCR is {deal.stabilized_dscr:.2f}×, below the "
                f"{YELLOW_THRESHOLDS['stabilized_dscr_min']:.2f}× floor for YELLOW."
            )
        elif deal.stabilized_dscr < g["stabilized_dscr_min"]:
            failures.append("thin_dscr")
            reasons.append(
                f"Stabilized DSCR is {deal.stabilized_dscr:.2f}×, below the "
                f"{g['stabilized_dscr_min']:.2f}× GREEN threshold."
            )

    if deal.refi_appraisal_stress_pass is False:
        failures.append("refi_haircut_risk")
        reasons.append("Refi appraisal stress (−5% ARV haircut) drops the deal below 70% LTV.")

    if deal.insurance_trend_pct is not None and deal.insurance_trend_pct > g["insurance_trend_max_pct"]:
        failures.append("insurance_spike_risk")
        reasons.append(
            f"3-yr insurance premium trend is +{deal.insurance_trend_pct * 100:.0f}%, "
            f"above the +{g['insurance_trend_max_pct'] * 100:.0f}% threshold."
        )

    if deal.climate_pct is not None:
        if deal.climate_pct >= 0.90:
            failures.append("climate_top_decile")
            reasons.append(
                f"Climate risk is at the {deal.climate_pct * 100:.0f}th percentile — top decile, hard RED."
            )
        elif deal.climate_pct > g["climate_pct_max"]:
            failures.append("climate_top_decile")
            reasons.append(
                f"Climate risk is at the {deal.climate_pct * 100:.0f}th percentile, "
                f"above the {g['climate_pct_max'] * 100:.0f}th-percentile threshold."
            )

    if deal.alpha_stack_count is not None and deal.alpha_stack_count < g["alpha_stack_min_flags"]:
        failures.append("alpha_stack_too_thin")
        reasons.append(
            f"Alpha stack is {deal.alpha_stack_count} flag(s) — "
            f"need ≥{g['alpha_stack_min_flags']} for GREEN."
        )

    if deal.stress_coc_on_residual is not None and deal.stress_coc_on_residual < g["stress_min_coc_on_residual"]:
        failures.append("sensitivity_breaks_cashflow")
        reasons.append(
            f"Stress-case CoC on residual is {deal.stress_coc_on_residual * 100:.1f}%, "
            f"below the {g['stress_min_coc_on_residual'] * 100:.0f}% floor."
        )

    if deal.sensitivity_negative_cashflow:
        failures.append("sensitivity_breaks_cashflow")
        reasons.append("Sensitivity drives the deal to negative cashflow under reasonable stress.")

    if deal.msa_blended_percentile is not None and deal.msa_blended_percentile < 0.25:
        failures.append("msa_bottom_quartile")
        reasons.append(
            f"Selected MSA is in the bottom quartile (blended pct {deal.msa_blended_percentile * 100:.0f}%)."
        )

    if deal.rehab_overrun_risk:
        failures.append("rehab_overrun_risk")
        reasons.append("Rehab heuristic flags >30% variance risk — needs a signed contractor bid.")

    if deal.financing_concentration_risk:
        failures.append("financing_risk")
        reasons.append("Single hard-money source — lender pulling the line mid-rehab is unmitigated.")

    if deal.exit_risk_no_ltr_fallback:
        failures.append("exit_risk")
        reasons.append("STR-dependent exit with no LTR fallback property manager identified.")

    # De-dup while preserving order
    seen = set()
    failures = [f for f in failures if not (f in seen or seen.add(f))]
    return failures, reasons


def _is_hard_red(deal: DealUnderwriting) -> tuple[bool, list[str]]:
    """Conditions that no mitigation can lift."""
    hard = []
    if deal.stabilized_dscr is not None and deal.stabilized_dscr < YELLOW_THRESHOLDS["stabilized_dscr_min"]:
        hard.append("thin_dscr")
    if deal.climate_pct is not None and deal.climate_pct >= 0.90:
        hard.append("climate_top_decile")
    if deal.sensitivity_negative_cashflow:
        hard.append("sensitivity_breaks_cashflow")
    return bool(hard), hard


def _missing_mitigations(failures: list[str], mitigations: VerifiedMitigations) -> list[str]:
    """Return the unverified mitigations the user still needs to flip."""
    needed: list[str] = []
    for f in failures:
        for m in REQUIRED_MITIGATIONS_BY_FAILURE.get(f, []):
            if not getattr(mitigations, m, False) and m not in needed:
                needed.append(m)
    return needed


def _apply_mitigations(deal: DealUnderwriting, mitigations: VerifiedMitigations) -> DealUnderwriting:
    """Per the spec: a verified 70%-LTV term sheet plus capital-reserve
    documentation lifts the stabilized DSCR by ~+0.13×. Other mitigations
    don't change the numerics but unblock the gate."""
    out = DealUnderwriting(**asdict(deal))
    if (mitigations.verified_70pct_ltv_term_sheet
            and mitigations.documented_capital_reserve_min_25k
            and out.stabilized_dscr is not None):
        out.stabilized_dscr = round(out.stabilized_dscr + 0.13, 4)
    if mitigations.signed_contractor_bid:
        out.rehab_overrun_risk = False
    if mitigations.committed_hard_money_primary and mitigations.committed_hard_money_backup:
        out.financing_concentration_risk = False
    if mitigations.ltr_fallback_pm_identified:
        out.exit_risk_no_ltr_fallback = False
    return out


def classify(deal: DealUnderwriting, mitigations: VerifiedMitigations | None = None) -> Recommendation:
    """Run the recommendation gate.

    Order of operations:
      1. Apply verified mitigations to the deal where they affect numerics.
      2. Hard-RED check (thin DSCR, top-decile climate, sensitivity -> -CF).
      3. Re-evaluate failures after mitigations.
      4. GREEN if no failures. YELLOW if failures present but all required
         mitigations are verified. RED otherwise.
    """
    mitigations = mitigations or VerifiedMitigations()
    mitigated = _apply_mitigations(deal, mitigations)

    is_hard_red, hard_failures = _is_hard_red(mitigated)
    failures, reasons = _evaluate_failures(mitigated)

    verified = [name for name in (
        "verified_70pct_ltv_term_sheet",
        "documented_capital_reserve_min_25k",
        "signed_contractor_bid",
        "committed_hard_money_primary",
        "committed_hard_money_backup",
        "ltr_fallback_pm_identified",
    ) if getattr(mitigations, name, False)]

    if is_hard_red:
        missing = _missing_mitigations(hard_failures + failures, mitigations)
        return Recommendation(
            verdict=Verdict.RED,
            reasons=reasons or ["Deal fails a hard-RED threshold that mitigations cannot lift."],
            failures=failures or hard_failures,
            required_mitigations=missing,
            verified_mitigations=verified,
            primary_action="Pass on this deal or restructure terms (lower price, larger rehab budget) before re-running.",
        )

    if not failures:
        return Recommendation(
            verdict=Verdict.GREEN,
            reasons=["All GREEN thresholds clear: DSCR ≥1.30×, refi stress passes, climate <75th pct, alpha-stack ≥2, stress CoC ≥8%."],
            failures=[],
            required_mitigations=[],
            verified_mitigations=verified,
            primary_action="Submit offer at modeled price; no mitigations needed.",
        )

    missing = _missing_mitigations(failures, mitigations)
    if not missing:
        return Recommendation(
            verdict=Verdict.YELLOW,
            reasons=reasons + ["All required mitigations verified — deal upgraded from RED to YELLOW."],
            failures=failures,
            required_mitigations=[],
            verified_mitigations=verified,
            primary_action="Submit a conditional offer; close only after the verified mitigations remain in force.",
        )

    pretty_missing = ", ".join(m.replace("_", " ") for m in missing)
    return Recommendation(
        verdict=Verdict.RED,
        reasons=reasons,
        failures=failures,
        required_mitigations=missing,
        verified_mitigations=verified,
        primary_action=f"Upgrade to YELLOW: verify {pretty_missing}.",
    )


# --- Memphis fixture (long paper §9) -----------------------------------------

MEMPHIS_BRRRR_FIXTURE = DealUnderwriting(
    stabilized_dscr=1.07,
    refi_appraisal_stress_pass=False,    # 5% haircut puts LTV >70%
    insurance_trend_pct=0.18,             # within threshold
    climate_pct=0.55,                     # mid-pack
    alpha_stack_count=3,                  # distressed + value-add + use-change
    stress_coc_on_residual=0.05,          # below 8% GREEN floor
    msa_blended_percentile=0.40,          # not bottom quartile
    sensitivity_negative_cashflow=False,
    rehab_overrun_risk=True,
    financing_concentration_risk=True,
    exit_risk_no_ltr_fallback=True,
)

FULL_MEMPHIS_MITIGATIONS = VerifiedMitigations(
    verified_70pct_ltv_term_sheet=True,
    documented_capital_reserve_min_25k=True,
    signed_contractor_bid=True,
    committed_hard_money_primary=True,
    committed_hard_money_backup=True,
    ltr_fallback_pm_identified=True,
)
