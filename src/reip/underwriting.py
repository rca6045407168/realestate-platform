"""Per-property underwriting workspace per §8 of the framework.

Computes:
  - Cap rate
  - Cash-on-cash return
  - DSCR (debt service coverage ratio)
  - 5-yr IRR & equity multiple
  - Sensitivity table over rent ±10%, vacancy ±200bp, exit cap ±100bp

Plus the BRRRR refinance module: given ARV and refi LTV, returns equity
recovered at refi and post-refi cash flow.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable
import math
import pandas as pd


@dataclass
class Assumptions:
    purchase_price: float
    rehab_cost: float = 0.0
    arv: float | None = None
    monthly_rent: float = 0.0
    vacancy: float = 0.05
    opex_ratio: float = 0.40            # opex incl. property mgmt, taxes, insurance, capex reserve
    property_tax_rate: float = 0.012     # of value, annual
    insurance_annual: float = 1500.0
    hoa_monthly: float = 0.0
    mortgage_rate: float = 0.07
    ltv: float = 0.75
    term_years: int = 30
    closing_cost_pct: float = 0.03
    refi_ltv: float = 0.75
    rent_growth: float = 0.03
    expense_growth: float = 0.03
    exit_cap: float = 0.06
    selling_cost_pct: float = 0.07
    hold_years: int = 5

    @property
    def all_in_cost(self) -> float:
        return self.purchase_price * (1 + self.closing_cost_pct) + self.rehab_cost


def _amortizing_payment(principal: float, annual_rate: float, term_years: int) -> float:
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    n = term_years * 12
    if r == 0:
        return principal / n
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def _annual_noi(a: Assumptions) -> float:
    gross_rent = a.monthly_rent * 12
    effective_gross = gross_rent * (1 - a.vacancy)
    operating_exp = (
        effective_gross * a.opex_ratio
        + a.property_tax_rate * a.purchase_price
        + a.insurance_annual
        + a.hoa_monthly * 12
    )
    return effective_gross - operating_exp


def proforma(a: Assumptions) -> dict:
    """Year-1 pro forma."""
    noi = _annual_noi(a)
    loan = a.purchase_price * a.ltv
    monthly_pi = _amortizing_payment(loan, a.mortgage_rate, a.term_years)
    annual_debt_service = monthly_pi * 12
    cash_flow = noi - annual_debt_service
    equity_invested = a.purchase_price * (1 - a.ltv) + a.purchase_price * a.closing_cost_pct + a.rehab_cost
    cap_rate = noi / a.purchase_price if a.purchase_price else 0
    cocr = cash_flow / equity_invested if equity_invested > 0 else float("nan")
    dscr = noi / annual_debt_service if annual_debt_service > 0 else float("inf")
    return {
        "noi": round(noi, 2),
        "annual_debt_service": round(annual_debt_service, 2),
        "cash_flow_y1": round(cash_flow, 2),
        "equity_invested": round(equity_invested, 2),
        "cap_rate": round(cap_rate, 4),
        "cash_on_cash": round(cocr, 4),
        "dscr": round(dscr, 2),
    }


def brrrr_refi(a: Assumptions) -> dict:
    """If ARV is set, compute refinance proceeds at refi_ltv and equity left in."""
    if a.arv is None:
        return {"applicable": False}
    new_loan = a.arv * a.refi_ltv
    payoff = a.purchase_price * a.ltv  # original loan balance (approx, ignoring amort)
    cash_out = new_loan - payoff
    equity_left_in = max(a.all_in_cost - new_loan, 0)
    new_payment = _amortizing_payment(new_loan, a.mortgage_rate, a.term_years)
    noi = _annual_noi(a)
    new_cash_flow = noi - new_payment * 12
    return {
        "applicable": True,
        "arv": a.arv,
        "new_loan": round(new_loan, 2),
        "cash_out_at_refi": round(cash_out, 2),
        "equity_left_in_after_refi": round(equity_left_in, 2),
        "post_refi_annual_cf": round(new_cash_flow, 2),
        "infinite_return": equity_left_in <= 0,
    }


def projection(a: Assumptions) -> pd.DataFrame:
    """Full hold-period projection."""
    rows = []
    rent = a.monthly_rent
    expenses_base = (
        a.opex_ratio * (a.monthly_rent * 12 * (1 - a.vacancy))
        + a.property_tax_rate * a.purchase_price
        + a.insurance_annual
        + a.hoa_monthly * 12
    )
    loan = a.purchase_price * a.ltv
    debt_payment = _amortizing_payment(loan, a.mortgage_rate, a.term_years) * 12
    expenses = expenses_base
    for yr in range(1, a.hold_years + 1):
        gross = rent * 12
        eff = gross * (1 - a.vacancy)
        opex = expenses
        noi = eff - opex
        cf = noi - debt_payment
        rows.append({"year": yr, "gross_rent": gross, "noi": noi, "debt_service": debt_payment, "cash_flow": cf})
        rent *= 1 + a.rent_growth
        expenses *= 1 + a.expense_growth
    df = pd.DataFrame(rows)
    return df


def irr(a: Assumptions) -> dict:
    """Hold-period IRR + equity multiple. Sale at year `hold_years` at exit
    cap on terminal NOI, net of selling cost."""
    proj = projection(a)
    terminal_noi = proj["noi"].iloc[-1] * (1 + a.rent_growth)  # next-year NOI
    sale_price = terminal_noi / a.exit_cap
    sale_proceeds = sale_price * (1 - a.selling_cost_pct)
    # Approximate ending loan balance via amortization
    loan = a.purchase_price * a.ltv
    n = a.term_years * 12
    r = a.mortgage_rate / 12
    paid = a.hold_years * 12
    if r == 0:
        balance = loan * (1 - paid / n)
    else:
        balance = loan * ((1 + r) ** n - (1 + r) ** paid) / ((1 + r) ** n - 1)
    net_sale_to_equity = sale_proceeds - balance
    cf = list(proj["cash_flow"].values)
    cf[-1] = cf[-1] + net_sale_to_equity
    equity = a.purchase_price * (1 - a.ltv) + a.purchase_price * a.closing_cost_pct + a.rehab_cost
    cashflows = [-equity] + cf

    def _npv(rate):
        return sum(c / (1 + rate) ** i for i, c in enumerate(cashflows))

    lo, hi = -0.99, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return {
        "irr": round(mid, 4),
        "equity_multiple": round(sum(cf) / equity, 3) if equity > 0 else float("nan"),
        "terminal_value": round(sale_price, 2),
        "ending_loan_balance": round(balance, 2),
        "net_sale_to_equity_y_n": round(net_sale_to_equity, 2),
    }


def sensitivity(a: Assumptions, rent_pcts: Iterable[float] = (-0.10, -0.05, 0, 0.05, 0.10),
                vacancies: Iterable[float] = (0.03, 0.05, 0.07, 0.10),
                exit_caps: Iterable[float] = (0.05, 0.06, 0.07)) -> pd.DataFrame:
    rows = []
    base_rent = a.monthly_rent
    for rp in rent_pcts:
        for vac in vacancies:
            for ec in exit_caps:
                a2 = Assumptions(**{**a.__dict__, "monthly_rent": base_rent * (1 + rp), "vacancy": vac, "exit_cap": ec})
                r = irr(a2)
                rows.append({
                    "rent_change": rp, "vacancy": vac, "exit_cap": ec,
                    "irr": r["irr"], "eq_mult": r["equity_multiple"],
                })
    return pd.DataFrame(rows)


def underwrite(a: Assumptions) -> dict:
    return {
        "assumptions": a.__dict__,
        "proforma_y1": proforma(a),
        "brrrr_refi": brrrr_refi(a),
        "irr": irr(a),
    }
