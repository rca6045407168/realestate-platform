"""5-year forward projection per property.

Given a listing + the MSA's appreciation thesis (from msa_score) + the
zip's ZHVI trajectory + ZORI rent + standard financing assumptions,
return:

  appreciation_5y_pct    — cumulative price appreciation over 5y
  appreciation_5y_dollars— dollar appreciation
  rental_profit_5y       — sum of (NOI − debt service) over 5y, with
                            rent and expense growth applied
  equity_paydown_5y      — mortgage principal paid over 5y
  total_return_5y        — sum of the three components net of equity
  irr_5y                 — levered IRR
  cash_on_cash_y1        — first-year cash-on-cash

The two distinct numbers the user asked for explicitly:
  - 5-yr rental profit
  - 5-yr appreciation projection
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math
from .underwriting import Assumptions, proforma, irr as _irr_calc


@dataclass
class Projection:
    appreciation_cagr: float
    appreciation_5y_pct: float
    appreciation_5y_dollars: float
    rental_profit_5y: float
    equity_paydown_5y: float
    total_return_5y_dollars: float
    total_return_5y_pct: float
    irr_5y: float
    cash_on_cash_y1: float
    dscr_y1: float
    cap_rate_y1: float
    vacancy_used: float          # the actual vacancy rate the projection used
    vacancy_source: str          # 'acs:zip-county' | 'default-5pct' | 'override'
    sources: list[str]


def _cagr(now: float, then: float, years: int) -> Optional[float]:
    if not (now and then) or then <= 0:
        return None
    try:
        return (now / then) ** (1 / years) - 1
    except Exception:
        return None


def _ending_balance(loan: float, rate: float, term_years: int, paid_months: int) -> float:
    if loan <= 0 or rate <= 0:
        return max(0.0, loan * (1 - paid_months / (term_years * 12)))
    r = rate / 12
    n = term_years * 12
    return loan * ((1 + r) ** n - (1 + r) ** paid_months) / ((1 + r) ** n - 1)


def _zhvi_cagr_for_zip(con, zip_code: str, years: int = 5) -> Optional[float]:
    """Pull the zip's ZHVI 5-yr trailing CAGR. Used as the appreciation
    *prior* — the forward projection assumes the zip continues at this
    pace, which is conservative vs. Sun Belt and aggressive vs. Cashflow
    Heartland. The MSA archetype overlay tempers it."""
    if not zip_code:
        return None
    rows = con.execute(
        """WITH t AS (
              SELECT period, value FROM zillow_zhvi WHERE zip = ? ORDER BY period DESC
           )
           SELECT (SELECT value FROM t LIMIT 1) AS now,
                  (SELECT value FROM t LIMIT 1 OFFSET ?) AS then_""",
        [str(zip_code).zfill(5), years * 12 - 1],
    ).fetchone()
    if not rows:
        return None
    return _cagr(rows[0], rows[1], years)


def _zhvi_now(con, zip_code: str) -> Optional[float]:
    if not zip_code:
        return None
    row = con.execute(
        "SELECT value FROM zillow_zhvi WHERE zip = ? ORDER BY period DESC LIMIT 1",
        [str(zip_code).zfill(5)],
    ).fetchone()
    return float(row[0]) if row else None


def _zori_now(con, zip_code: str) -> Optional[float]:
    if not zip_code:
        return None
    row = con.execute(
        "SELECT value FROM zillow_zori WHERE zip = ? ORDER BY period DESC LIMIT 1",
        [str(zip_code).zfill(5)],
    ).fetchone()
    return float(row[0]) if row else None


def _rental_vacancy_for_zip(con, zip_code: str) -> Optional[float]:
    """ACS-derived rental vacancy rate for the zip's county. The HVS
    formula: vacant_for_rent / (vacant_for_rent + renter_occupied).
    Returns None if the underlying ACS columns are missing or zero.
    """
    if not zip_code:
        return None
    row = con.execute(
        """SELECT a.vacant_for_rent, a.renter_occupied
           FROM zip_county_xwalk z
           JOIN acs_county a ON a.fips_county = z.fips_county
           WHERE z.zip = ? AND a.year = (SELECT MAX(year) FROM acs_county)
           ORDER BY a.year DESC LIMIT 1""",
        [str(zip_code).zfill(5)],
    ).fetchone()
    if not row:
        return None
    vacant, renter = row
    if vacant is None or renter is None:
        return None
    denom = float(vacant) + float(renter)
    if denom <= 0:
        return None
    pct = float(vacant) / denom
    # Clamp implausible values — ACS noise on small zips can put rentals at
    # 0 or 100% vacancy. Cap at 30% (catastrophic but real for some markets)
    # and floor at 2% (the structural friction floor).
    return max(0.02, min(0.30, pct))


def _msa_appreciation_overlay(archetype: Optional[str]) -> float:
    """Multiplier on the zip's trailing appreciation CAGR based on the
    MSA archetype. Cashflow Heartland markets are mean-reverting; Sun Belt
    can extrapolate; Boom-Bust is haircut.
    """
    return {
        "Coastal Gateway":     0.85,   # haircut high coastal CAGRs
        "Sun Belt Growth":     1.00,   # extrapolate
        "Cashflow Heartland":  0.75,   # mean-revert downward
        "Boom-Bust Beta":      0.60,   # heavy haircut
        "Resource & Niche":    0.80,
        "Mixed":               0.85,
    }.get(archetype or "Mixed", 0.85)


def project(
    con,
    listing: dict,
    archetype: Optional[str] = None,
    mortgage_rate: float = 0.07,
    ltv: float = 0.75,
    vacancy: Optional[float] = None,
    opex_ratio: float = 0.40,
    property_tax_rate: float = 0.012,
    insurance_annual: float = 1500.0,
    rent_growth: float = 0.03,
    expense_growth: float = 0.03,
    closing_cost_pct: float = 0.03,
    hold_years: int = 5,
) -> Projection:
    sources: list[str] = []
    price = float(listing["listed_price"])
    zip_code = listing.get("zip")

    # Vacancy: prefer the ACS-derived rental vacancy for the zip's county.
    # Fall back to a 5% default. Caller can pass `vacancy` to force an
    # override (e.g. to model the user's local PM's actual experience).
    if vacancy is not None:
        vacancy_used = vacancy
        vacancy_source = "override"
    else:
        acs_vac = _rental_vacancy_for_zip(con, zip_code)
        if acs_vac is not None:
            vacancy_used = acs_vac
            vacancy_source = "acs:zip-county"
            sources.append(f"vacancy: ACS rental vacancy {acs_vac*100:.1f}% (zip→county)")
        else:
            vacancy_used = 0.05
            vacancy_source = "default-5pct"
            sources.append("vacancy: default 5% (no ACS coverage for this zip)")
    vacancy = vacancy_used

    # Rent: prefer ZORI for the zip; if missing, estimate at 0.8% of value
    # (the classic 1% rule, conservatively haircut). Real Mid-West yields
    # land between 0.7% and 1.0% of value monthly.
    rent = _zori_now(con, zip_code)
    if rent:
        sources.append("ZORI:zip")
    else:
        rent = price * 0.008
        sources.append("rule-of-thumb 0.8% rent:price")

    # Appreciation prior: zip's trailing 5y ZHVI CAGR, tempered by
    # archetype overlay so we don't extrapolate Cashflow Heartland up.
    raw_cagr = _zhvi_cagr_for_zip(con, zip_code, years=5)
    if raw_cagr is None:
        # National long-run housing real return ~ 1.5%; nominal ~ 4%
        raw_cagr = 0.04
        sources.append("appreciation: long-run national prior 4%")
    else:
        sources.append("appreciation: ZHVI 5y CAGR for zip")
    overlay = _msa_appreciation_overlay(archetype)
    appr_cagr = raw_cagr * overlay
    if appr_cagr > 0.10:    # cap forward projections at 10%
        appr_cagr = 0.10
    if appr_cagr < -0.05:   # floor at -5%
        appr_cagr = -0.05

    # Build the assumptions and run the existing pro forma machinery for
    # consistency with the rec gate.
    a = Assumptions(
        purchase_price=price,
        rehab_cost=0.0,
        arv=price,
        monthly_rent=rent,
        mortgage_rate=mortgage_rate, ltv=ltv,
        vacancy=vacancy, opex_ratio=opex_ratio,
        property_tax_rate=property_tax_rate,
        insurance_annual=insurance_annual,
        hoa_monthly=float(listing.get("hoa_monthly") or 0),
        rent_growth=rent_growth, expense_growth=expense_growth,
        exit_cap=0.07,    # not used for total-return calc; we use direct appr
        selling_cost_pct=0.07,
        closing_cost_pct=closing_cost_pct,
        hold_years=hold_years,
    )
    pf = proforma(a)

    # 5y rental profit = sum of (NOI − debt service) with rent + expense growth
    rent_t = rent
    expenses = (
        opex_ratio * (rent * 12 * (1 - vacancy))
        + property_tax_rate * price + insurance_annual
        + (a.hoa_monthly * 12)
    )
    debt_service = pf["annual_debt_service"]
    cf = []
    for _ in range(hold_years):
        eff = rent_t * 12 * (1 - vacancy)
        noi = eff - expenses
        cf.append(noi - debt_service)
        rent_t  *= 1 + rent_growth
        expenses *= 1 + expense_growth
    rental_profit_5y = sum(cf)

    # 5y appreciation
    appreciation_5y_pct = (1 + appr_cagr) ** hold_years - 1
    appreciation_5y_dollars = price * appreciation_5y_pct

    # 5y equity paydown
    loan = price * ltv
    end_balance = _ending_balance(loan, mortgage_rate, 30, hold_years * 12)
    equity_paydown_5y = max(0.0, loan - end_balance)

    # IRR: original equity in, 5 yrs of CF, plus net sale proceeds
    sale_price = price * (1 + appreciation_5y_pct)
    selling_cost_pct = 0.07
    net_sale = sale_price * (1 - selling_cost_pct) - end_balance
    cf_irr = list(cf)
    cf_irr[-1] += net_sale
    equity_in = pf["equity_invested"]
    cashflows = [-equity_in] + cf_irr

    def npv(rate):
        return sum(c / (1 + rate) ** i for i, c in enumerate(cashflows))

    lo, hi = -0.99, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    irr_5y = mid
    if math.isnan(irr_5y) or math.isinf(irr_5y):
        irr_5y = 0.0

    total_return_5y = rental_profit_5y + appreciation_5y_dollars + equity_paydown_5y
    total_return_5y_pct = total_return_5y / equity_in if equity_in > 0 else 0.0

    return Projection(
        appreciation_cagr=round(appr_cagr, 4),
        appreciation_5y_pct=round(appreciation_5y_pct, 4),
        appreciation_5y_dollars=round(appreciation_5y_dollars, 2),
        rental_profit_5y=round(rental_profit_5y, 2),
        equity_paydown_5y=round(equity_paydown_5y, 2),
        total_return_5y_dollars=round(total_return_5y, 2),
        total_return_5y_pct=round(total_return_5y_pct, 4),
        irr_5y=round(irr_5y, 4),
        cash_on_cash_y1=pf["cash_on_cash"],
        dscr_y1=pf["dscr"],
        cap_rate_y1=pf["cap_rate"],
        vacancy_used=round(vacancy_used, 4),
        vacancy_source=vacancy_source,
        sources=sources,
    )
