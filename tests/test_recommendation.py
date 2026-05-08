"""Memphis fixture acceptance tests — the recommendation gate's moral center."""
from reip import recommendation as rec


def test_memphis_default_rules_screen_RED():
    """1.07x DSCR with no mitigations must be RED — even by a hair. Without
    that consistency, the gate becomes a soft suggestion."""
    out = rec.classify(rec.MEMPHIS_BRRRR_FIXTURE)
    assert out.verdict == rec.Verdict.RED
    assert "thin_dscr" in out.failures
    # The user gets a list of required mitigations, not a soft pass.
    assert "verified_70pct_ltv_term_sheet" in out.required_mitigations
    assert "documented_capital_reserve_min_25k" in out.required_mitigations


def test_memphis_with_all_mitigations_upgrades_to_YELLOW():
    """All five (six counting the HM-backup pair) verified mitigations
    upgrade RED → YELLOW. The +0.13x DSCR boost from the term sheet +
    reserves makes 1.07x → 1.20x, clearing the YELLOW floor (1.10x)."""
    out = rec.classify(rec.MEMPHIS_BRRRR_FIXTURE, rec.FULL_MEMPHIS_MITIGATIONS)
    assert out.verdict == rec.Verdict.YELLOW
    assert out.required_mitigations == []
    assert all(
        m in out.verified_mitigations for m in [
            "verified_70pct_ltv_term_sheet",
            "documented_capital_reserve_min_25k",
            "signed_contractor_bid",
            "committed_hard_money_primary",
            "committed_hard_money_backup",
            "ltr_fallback_pm_identified",
        ]
    )


def test_memphis_partial_mitigations_stays_RED():
    """Three of the six — not enough; gate stays RED."""
    partial = rec.VerifiedMitigations(
        verified_70pct_ltv_term_sheet=True,
        documented_capital_reserve_min_25k=True,
        signed_contractor_bid=True,
        # Missing: committed_hard_money_*, ltr_fallback_pm_identified
    )
    out = rec.classify(rec.MEMPHIS_BRRRR_FIXTURE, partial)
    assert out.verdict == rec.Verdict.RED
    assert any("hard money" in m.replace("_", " ") for m in out.required_mitigations)


def test_clean_deal_screens_GREEN():
    deal = rec.DealUnderwriting(
        stabilized_dscr=1.45,
        refi_appraisal_stress_pass=True,
        insurance_trend_pct=0.05,
        climate_pct=0.30,
        alpha_stack_count=3,
        stress_coc_on_residual=0.12,
        msa_blended_percentile=0.78,
        sensitivity_negative_cashflow=False,
    )
    out = rec.classify(deal)
    assert out.verdict == rec.Verdict.GREEN
    assert out.required_mitigations == []


def test_top_decile_climate_is_hard_red():
    """Climate ≥90th percentile cannot be mitigated away."""
    deal = rec.DealUnderwriting(
        stabilized_dscr=1.45, refi_appraisal_stress_pass=True,
        insurance_trend_pct=0.05, climate_pct=0.93,
        alpha_stack_count=3, stress_coc_on_residual=0.12,
        msa_blended_percentile=0.78,
    )
    out = rec.classify(deal, rec.FULL_MEMPHIS_MITIGATIONS)
    assert out.verdict == rec.Verdict.RED
    assert "climate_top_decile" in out.failures


def test_negative_cashflow_under_stress_is_hard_red():
    deal = rec.DealUnderwriting(
        stabilized_dscr=1.45, refi_appraisal_stress_pass=True,
        insurance_trend_pct=0.05, climate_pct=0.30,
        alpha_stack_count=3, stress_coc_on_residual=0.05,
        sensitivity_negative_cashflow=True, msa_blended_percentile=0.5,
    )
    out = rec.classify(deal, rec.FULL_MEMPHIS_MITIGATIONS)
    assert out.verdict == rec.Verdict.RED
