"""Tax-adjusted real estate underwriting.

Real estate's structural edge over equities is depreciation: the IRS lets
you write down the building (not land) over 27.5 years, generating a
non-cash expense that creates a *paper loss* in many years even when the
property is cash-flow positive.

Whether you actually USE that paper loss depends on your tax situation:

  - Passive-loss limited (most W-2 earners with AGI > $150K): the loss
    carries forward and offsets gains at sale, but doesn't reduce current
    income tax. Cash-flow impact: ~0 per year, but exit IRR gets a bump
    because the suspended losses release at disposition.

  - Real-estate-professional / Active-host loophole / High-volume STR
    (the bonus depreciation crowd): full deduction against ordinary
    income each year. Cash-flow impact: tax_bracket × depreciation per
    year — the biggest annual tax shield available to a non-business owner.

  - $25K active-participant special allowance (AGI ≤ $100K, phasing out
    at $150K): up to $25K/yr of rental loss against ordinary income.

This module models the "active deduction" case explicitly because it's the
upper-bound benefit. Investors with passive-loss limits should treat the
post-tax-IRR uplift as the OPTIMISTIC case.

Standard knobs:
  - useful_life_years: 27.5 (residential) — sets annual depreciation
  - land_allocation:   0.20 (default; per-county varies, IRS uses tax-
                       assessment ratio) — only building depreciates
  - tax_bracket:       marginal rate (default 32% — high earner)
  - recapture_rate:    25% at sale (Section 1250)

This module does not handle: state tax, 1031 exchanges, opportunity
zones, bonus depreciation, cost segregation, or AMT. Those are real but
investor-specific — keep the gate honest and surface them as caveats.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ---- defaults --------------------------------------------------------------

DEFAULT_TAX_BRACKET    = 0.32   # high earner: 32% federal marginal
DEFAULT_LAND_ALLOC     = 0.20   # 20% of price = land, not depreciable
DEFAULT_USEFUL_LIFE_Y  = 27.5   # IRS residential rental
DEFAULT_RECAPTURE      = 0.25   # Section 1250


@dataclass
class TaxAssumptions:
    tax_bracket:        float = DEFAULT_TAX_BRACKET
    land_allocation:    float = DEFAULT_LAND_ALLOC
    useful_life_years:  float = DEFAULT_USEFUL_LIFE_Y
    recapture_rate:     float = DEFAULT_RECAPTURE
    # If False, treat depreciation as suspended (passive-loss limited).
    # Cash flow doesn't change; exit gets a one-time boost. Set True for
    # real-estate-professionals / STR active-host / sub-$100K active
    # participant.
    deduction_against_ordinary: bool = True


def annual_depreciation(purchase_price: float, rehab_cost: float = 0,
                        land_allocation: float = DEFAULT_LAND_ALLOC,
                        useful_life_years: float = DEFAULT_USEFUL_LIFE_Y) -> float:
    """Straight-line depreciation on the building basis (price + rehab − land)."""
    basis = (purchase_price + rehab_cost) * (1 - land_allocation)
    return basis / useful_life_years


def annual_tax_savings(pretax_cf: float, depreciation: float,
                        tax: "TaxAssumptions" = None) -> float:
    """Tax saved THIS YEAR by depreciation, relative to a no-depreciation baseline.

    For a cash-positive deal, this is the bracket × min(pretax_cf, depreciation),
    plus (in active case) the bracket × paper-loss that offsets ordinary income.

    For a cash-negative deal, the no-depreciation baseline owes zero tax
    (you can't be taxed on a loss), so the savings is the bracket × |paper_loss|
    if active, else 0 (passive: suspended).

    Always ≥ 0. The intuition: this is what you'd brag about at a poker table.
    """
    tax = tax or TaxAssumptions()
    # Without depreciation:
    tax_without_depreciation = max(0.0, pretax_cf) * tax.tax_bracket
    # With depreciation:
    after_depr = pretax_cf - depreciation
    if tax.deduction_against_ordinary:
        tax_with_depreciation = after_depr * tax.tax_bracket   # can be negative (refund)
    else:
        tax_with_depreciation = max(0.0, after_depr) * tax.tax_bracket
    return tax_without_depreciation - tax_with_depreciation


def post_tax_annual_cf(pretax_cf: float, depreciation: float,
                       tax: TaxAssumptions = None) -> float:
    """Post-tax annual cash flow.

    Pretax CF is what your bank account sees (NOI minus debt service).
    Tax owed depends on taxable income, which is pretax_cf minus the
    depreciation deduction:

      - Active (deduction_against_ordinary=True): the full depreciation
        deducts against ordinary income. Tax_owed can go negative when
        depreciation exceeds CF — that's a refund / offset against your
        W-2 or other income. Net CF goes UP relative to pretax (rare but
        real for cost-seg / bonus depreciation cases).
      - Passive: depreciation shields pretax_cf down to zero only; any
        excess paper loss is suspended (carries to disposition).
    """
    tax = tax or TaxAssumptions()
    if tax.deduction_against_ordinary:
        tax_owed = (pretax_cf - depreciation) * tax.tax_bracket
    else:
        tax_owed = max(0.0, pretax_cf - depreciation) * tax.tax_bracket
    return pretax_cf - tax_owed


def equity_at_exit_after_tax(sale_price: float, all_in_basis: float,
                              total_depreciation_taken: float,
                              tax: TaxAssumptions = TaxAssumptions()) -> dict:
    """Exit math with recapture + capital gains.

    Returns gross sale proceeds, depreciation recapture tax (Sec 1250),
    capital gains tax (LTCG 20% assumed for high earner), and net to
    equity.

    Simplification: we lump CG at 20% (high earner without NIIT). State
    tax not included. Bonus depreciation recapture (Sec 1245 ordinary
    rate) not separately handled. These are real numbers but require
    investor-specific tax advice — the platform surfaces magnitudes,
    not Schedule D line items.
    """
    adj_basis = max(all_in_basis - total_depreciation_taken, 0)
    total_gain = sale_price - adj_basis
    recapture_gain = min(total_depreciation_taken, total_gain)
    capgain = max(total_gain - recapture_gain, 0)
    recap_tax = recapture_gain * tax.recapture_rate
    LTCG_RATE = 0.20
    cg_tax = capgain * LTCG_RATE
    return {
        "adjusted_basis":   round(adj_basis, 2),
        "total_gain":       round(total_gain, 2),
        "recapture_gain":   round(recapture_gain, 2),
        "capital_gain":     round(capgain, 2),
        "recapture_tax":    round(recap_tax, 2),
        "capital_gain_tax": round(cg_tax, 2),
        "total_exit_tax":   round(recap_tax + cg_tax, 2),
    }


def irr_uplift_estimate(pretax_irr: float, pretax_annual_cf: float,
                        depreciation: float, equity_invested: float,
                        hold_years: int = 5,
                        tax: TaxAssumptions = TaxAssumptions()) -> float:
    """Estimate the post-tax IRR by computing the annual tax shield as a
    yield bump on equity, then adding to pretax IRR.

    Limitations:
      - Annualizes the shield rather than running a full year-by-year
        post-tax cash-flow ladder. Accurate within ~50bps for hold ≤ 7y.
      - Ignores recapture at exit. The exit-tax drag would shave ~50-150bps
        off this number for a typical 5y hold; surface as a caveat in the
        UI, not adjusted here.
    """
    if equity_invested <= 0 or pretax_irr is None:
        return pretax_irr
    posttax_cf = post_tax_annual_cf(pretax_annual_cf, depreciation, tax)
    delta_cf = posttax_cf - pretax_annual_cf
    if delta_cf == 0:
        return pretax_irr
    yield_bump = delta_cf / equity_invested
    return pretax_irr + yield_bump
