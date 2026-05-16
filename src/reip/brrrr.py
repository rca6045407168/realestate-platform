"""BRRRR (Buy-Rehab-Rent-Refinance-Repeat) refinance walkthrough.

Implements the cash-flow walkthrough from `RealEstate_Investment_Framework_v5`
Section 9.2 (Memphis sample property). This is the module that turns the
prose example into auditable numbers:

  Acquisition + closing  $115,000 × 1.035        = $119,025
  Rehab spend                                    + $42,000
  Holding cost (4-month rehab + lease-up)        + $4,000
  ─────────────────────────────────────────────────────────
  Total invested before refi                     = $165,025

  Stabilized NOI       (= rent × 12 − opex)      = $13,800
  Refi @ 75% LTV       (= ARV × refi_ltv)        = $161,250
  Refi closing costs                             −  $3,500
  ─────────────────────────────────────────────────────────
  Net cash returned at refi                      = $157,750

  Residual capital     (= invested − returned)   =  $7,250
  Annual cashflow after debt service             =    $930
  Stabilized DSCR      (= NOI / annual P&I)      =  1.07x
  Cash-on-cash on residual                       = 13%

Distinct from `underwriting.brrrr_refi` (which models purchase financing
+ refi only, no holding cost, no refi closing cost). This module is the
canonical Section-9.2 walkthrough. The existing brrrr_refi stays for
back-compat — callers that need the v5 numbers use this module.

Build-spec reference: PLATFORM_BUILD_SPEC.md §3 Phase 3 — Memphis
fixture must produce $7,250 residual / 13% CoC base case within $50.
"""
from __future__ import annotations
from dataclasses import dataclass

from . import underwriting as uw


@dataclass
class BRRRRInputs:
    """Inputs for the v5 §9.2 walkthrough. Closing-cost percentages have
    defaults from the long paper; holding_cost is explicit because real
    deals price it from hard-money rate × months, not a static %."""
    purchase_price: float
    rehab_cost: float
    arv: float
    monthly_rent: float
    annual_opex: float                  # all-in opex (taxes, ins, mgmt, vac, capex, repairs)
    holding_cost: float                 # hard-money interest + carry during rehab+lease-up
    closing_cost_pct: float = 0.035     # 3.5% of purchase (v5 §9.2 default)
    refi_closing_cost: float = 3500.0   # $ amount, not pct (v5 §9.2 default)
    refi_ltv: float = 0.75
    refi_rate: float = 0.07
    refi_term_years: int = 30


@dataclass
class BRRRROutcome:
    """Output of the v5 §9.2 walkthrough. All $ amounts in dollars."""
    # Investment side
    acquisition_with_closing: float
    rehab_spend: float
    holding_cost: float
    total_invested_before_refi: float

    # Stabilized economics
    stabilized_noi: float
    stabilized_cap_rate: float          # NOI / ARV (the lender's view)

    # Refi side
    refi_proceeds_gross: float
    refi_closing_cost: float
    refi_proceeds_net: float
    residual_capital: float             # invested − returned. Negative = infinite return.
    annual_debt_service: float
    annual_cashflow_after_debt_service: float
    stabilized_dscr: float              # NOI / annual P&I — the gate's key input
    cash_on_cash_on_residual: float | None  # None when residual ≤ 0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        # Round for clean JSON; keep CoC as None if residual was ≤0.
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 2 if k != "cash_on_cash_on_residual" else 4)
        return d


def compute_brrrr(i: BRRRRInputs) -> BRRRROutcome:
    """Run the v5 §9.2 walkthrough end-to-end."""
    # Investment side
    acquisition = i.purchase_price * (1.0 + i.closing_cost_pct)
    total_invested = acquisition + i.rehab_cost + i.holding_cost

    # Stabilized economics — NOI is rent × 12 minus all-in opex.
    annual_rent = i.monthly_rent * 12.0
    noi = annual_rent - i.annual_opex
    cap_rate = noi / i.arv if i.arv > 0 else 0.0

    # Refi side
    refi_gross = i.arv * i.refi_ltv
    refi_net = refi_gross - i.refi_closing_cost
    residual = total_invested - refi_net

    # Debt service on the refi balance
    monthly_pi = uw._amortizing_payment(refi_gross, i.refi_rate, i.refi_term_years)
    annual_ds = monthly_pi * 12.0
    annual_cf = noi - annual_ds
    dscr = noi / annual_ds if annual_ds > 0 else float("inf")

    # CoC on residual capital. Convention: if residual ≤ 0, the BRRRR is
    # "infinite return" — we encode that as None so downstream code
    # doesn't divide by ~0 and produce nonsense.
    coc = (annual_cf / residual) if residual > 1.0 else None

    return BRRRROutcome(
        acquisition_with_closing=acquisition,
        rehab_spend=i.rehab_cost,
        holding_cost=i.holding_cost,
        total_invested_before_refi=total_invested,
        stabilized_noi=noi,
        stabilized_cap_rate=cap_rate,
        refi_proceeds_gross=refi_gross,
        refi_closing_cost=i.refi_closing_cost,
        refi_proceeds_net=refi_net,
        residual_capital=residual,
        annual_debt_service=annual_ds,
        annual_cashflow_after_debt_service=annual_cf,
        stabilized_dscr=dscr,
        cash_on_cash_on_residual=coc,
    )
