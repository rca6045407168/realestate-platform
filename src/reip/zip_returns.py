"""Per-zip 5y expected return projection across the entire United States.

The live-listings flow is gated to 11 metros where we have verified Redfin
region IDs. This module covers the other ~600 MSAs by computing expected
return at the *zip* level using only data we have for every US zip:

  - Zillow ZHVI: typical home value per zip (monthly history)
  - Zillow ZORI: observed rent index per zip (monthly history)
  - ACS B25004 / B25003: rental vacancy rate via zip→county join

For each zip we compute the same projection objects the per-property flow
uses (proj_mod.project) but with synthetic listing inputs derived from
ZHVI (price) and ZORI (rent). The result is "what would the typical home
in this zip return?" — useful for ranking the country and surfacing
which zips deserve a manual deep-dive on Redfin/Zillow.

Returned per zip:
  - zip, city/state/CBSA
  - typical price (ZHVI)
  - typical rent (ZORI)
  - 5y appreciation %, $
  - 5y rental profit $
  - 5y total return $, %
  - 5y IRR
  - DSCR / cap rate / vacancy used
  - Redfin and Zillow search URLs for the zip
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
import pandas as pd
from .store import connect
from . import projection as proj_mod


@dataclass
class ZipReturn:
    zip: str
    state: Optional[str]
    cbsa_code: Optional[str]
    cbsa_name: Optional[str]
    typical_price: float
    typical_rent: float
    appreciation_cagr: float          # forward projection (momentum-aware blend)
    appreciation_cagr_5y_trail: float # trailing 5y CAGR (raw)
    appreciation_cagr_2y_trail: float # trailing 2y CAGR (raw)
    chg_12mo: float                   # last 12 months change (sanity check)
    appreciation_5y_pct: float
    appreciation_5y_dollars: float
    rental_profit_5y: float
    total_return_5y_dollars: float
    total_return_5y_pct: float
    irr_5y: float
    cap_rate_y1: float
    dscr_y1: float
    vacancy_used: float
    archetype_hint: Optional[str]
    redfin_search_url: str
    zillow_search_url: str


QUERY = """
WITH zhvi_ranked AS (
    SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
    FROM zillow_zhvi
),
zhvi_now AS  (SELECT zip, value AS zhvi        FROM zhvi_ranked WHERE rn = 1),
zhvi_12 AS   (SELECT zip, value AS zhvi_12mo   FROM zhvi_ranked WHERE rn = 13),
zhvi_24 AS   (SELECT zip, value AS zhvi_24mo   FROM zhvi_ranked WHERE rn = 25),
zhvi_60 AS   (SELECT zip, value AS zhvi_5y_ago FROM zhvi_ranked WHERE rn = 60),
zori_now AS (
    SELECT zip, value AS zori
    FROM (SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn FROM zillow_zori)
    WHERE rn = 1
),
vacancy_per_zip AS (
    SELECT z.zip,
           a.vacant_for_rent / NULLIF(a.vacant_for_rent + a.renter_occupied, 0) AS vac
    FROM zip_county_xwalk z
    JOIN acs_county a ON a.fips_county = z.fips_county
    WHERE a.year = (SELECT MAX(year) FROM acs_county)
)
SELECT zhvi_now.zip,
       zhvi_now.zhvi,
       zhvi_12.zhvi_12mo,
       zhvi_24.zhvi_24mo,
       zhvi_60.zhvi_5y_ago,
       zori_now.zori,
       vacancy_per_zip.vac,
       c.cbsa_code, c.cbsa_name, c.state
FROM zhvi_now
JOIN zori_now USING (zip)
LEFT JOIN zhvi_12 USING (zip)
LEFT JOIN zhvi_24 USING (zip)
LEFT JOIN zhvi_60 USING (zip)
LEFT JOIN vacancy_per_zip USING (zip)
LEFT JOIN zip_county_xwalk z ON z.zip = zhvi_now.zip
LEFT JOIN county_cbsa_xwalk c ON c.fips_county = z.fips_county
WHERE zhvi_now.zhvi BETWEEN ? AND ?
  AND zori_now.zori BETWEEN 400 AND 8000
"""


def rank_us(
    con,
    min_price: int = 50_000,
    max_price: int = 800_000,
    mortgage_rate: float = 0.07,
    ltv: float = 0.75,
    state: Optional[str] = None,
    cbsa_code: Optional[str] = None,
    sort: str = "irr",
    limit: int = 100,
    archetypes_by_cbsa: dict | None = None,
) -> list[ZipReturn]:
    """Score every US zip with ZHVI+ZORI coverage and return the top N
    by chosen criterion.

    archetypes_by_cbsa is an optional {cbsa_code: archetype} dict so we can
    apply the framework's archetype overlay to each zip's appreciation prior.
    """
    df = con.execute(QUERY, [min_price, max_price]).df()
    if state:
        df = df[df["state"].str.contains(state, case=False, na=False)]
    if cbsa_code:
        df = df[df["cbsa_code"].astype(str) == str(cbsa_code)]
    if df.empty:
        return []

    archetypes_by_cbsa = archetypes_by_cbsa or {}
    overlay_by_arch = {
        "Coastal Gateway":     0.85,
        "Sun Belt Growth":     1.00,
        "Cashflow Heartland":  0.75,
        "Boom-Bust Beta":      0.60,
        "Resource & Niche":    0.80,
        "Mixed":               0.85,
    }

    out: list[ZipReturn] = []
    HOLD_YEARS = 5
    OPEX_RATIO = 0.40
    PROP_TAX = 0.012
    INSURANCE = 1500.0
    RENT_GROWTH = 0.03
    EXPENSE_GROWTH = 0.03
    CLOSING_COST_PCT = 0.03
    SELLING_COST_PCT = 0.07

    for r in df.itertuples():
        price = float(r.zhvi)
        rent  = float(r.zori)
        zhvi_5y = r.zhvi_5y_ago
        zhvi_2y = r.zhvi_24mo
        zhvi_12mo = r.zhvi_12mo

        # Compute multiple trailing CAGRs so we can be momentum-aware.
        # Florida 2026 case: 5y CAGR is +5–8% but last 12mo is -5 to -14%.
        # A pure 5y projection extrapolates the 2021-22 boom forward; that's
        # the bias the user's friend (correctly) called out.
        cagr_5y = (price / float(zhvi_5y)) ** (1/5) - 1 if (zhvi_5y is not None and not pd.isna(zhvi_5y) and zhvi_5y > 0) else None
        cagr_2y = (price / float(zhvi_2y)) ** (1/2) - 1 if (zhvi_2y is not None and not pd.isna(zhvi_2y) and zhvi_2y > 0) else None
        chg_12mo = (price / float(zhvi_12mo)) - 1   if (zhvi_12mo is not None and not pd.isna(zhvi_12mo) and zhvi_12mo > 0) else None

        # Forward-projection blend: weight recent more.
        if cagr_2y is not None and cagr_5y is not None:
            raw_cagr = 0.60 * cagr_2y + 0.40 * cagr_5y
        elif cagr_5y is not None:
            raw_cagr = cagr_5y
        elif cagr_2y is not None:
            raw_cagr = cagr_2y
        else:
            raw_cagr = 0.04   # national long-run prior

        # If the last 12mo is sharply negative (>5% drop), force the projection
        # to acknowledge the rollover — don't let the 5y boom anchor.
        if chg_12mo is not None and chg_12mo < -0.05:
            raw_cagr = min(raw_cagr, chg_12mo)   # honor the recent crash

        archetype = archetypes_by_cbsa.get(str(r.cbsa_code) if r.cbsa_code else None) or "Mixed"
        appr_cagr = max(-0.05, min(0.10, raw_cagr * overlay_by_arch.get(archetype, 0.85)))
        appr_5y_pct = (1 + appr_cagr) ** HOLD_YEARS - 1
        appr_5y_dollars = price * appr_5y_pct

        # Vacancy: ACS-derived if present (clamped to [2%, 30%]), else 5%
        if r.vac is None or pd.isna(r.vac):
            vacancy = 0.05
        else:
            vacancy = max(0.02, min(0.30, float(r.vac)))

        # Pro forma year 1
        gross_rent = rent * 12
        eff = gross_rent * (1 - vacancy)
        opex = eff * OPEX_RATIO + PROP_TAX * price + INSURANCE
        noi_y1 = eff - opex
        loan = price * ltv
        # 30-yr fixed monthly P&I
        rmo = mortgage_rate / 12
        n = 30 * 12
        if rmo == 0:
            mo_pi = loan / n
        else:
            mo_pi = loan * (rmo * (1 + rmo) ** n) / ((1 + rmo) ** n - 1)
        debt_service = mo_pi * 12
        cash_flow_y1 = noi_y1 - debt_service
        equity = price * (1 - ltv) + price * CLOSING_COST_PCT
        cap_rate = noi_y1 / price if price else 0
        dscr = noi_y1 / debt_service if debt_service else float("inf")

        # 5y rental profit (apply rent + expense growth)
        rt = rent
        ex = OPEX_RATIO * (rent * 12 * (1 - vacancy)) + PROP_TAX * price + INSURANCE
        cf = []
        for _ in range(HOLD_YEARS):
            e = rt * 12 * (1 - vacancy)
            yr_noi = e - ex
            cf.append(yr_noi - debt_service)
            rt *= 1 + RENT_GROWTH
            ex *= 1 + EXPENSE_GROWTH
        rental_profit_5y = sum(cf)

        # IRR via bisection on 5y CF + sale proceeds at terminal cap
        sale_price = price * (1 + appr_5y_pct)
        # Ending loan balance after 60 months
        if rmo == 0:
            balance = loan * (1 - 60 / n)
        else:
            balance = loan * ((1 + rmo) ** n - (1 + rmo) ** 60) / ((1 + rmo) ** n - 1)
        net_sale = sale_price * (1 - SELLING_COST_PCT) - balance
        cf_with_sale = list(cf)
        cf_with_sale[-1] += net_sale
        # bisect IRR
        lo, hi = -0.99, 5.0
        for _ in range(120):
            mid = (lo + hi) / 2
            npv = -equity + sum(c / (1 + mid) ** (i + 1) for i, c in enumerate(cf_with_sale))
            if npv > 0: lo = mid
            else:       hi = mid
        irr_5y = (lo + hi) / 2
        if not (-0.99 < irr_5y < 5.0):
            continue

        equity_paydown_5y = max(0.0, loan - balance)
        total_5y = rental_profit_5y + appr_5y_dollars + equity_paydown_5y

        # JSON safety: cap dscr at 999 (it goes inf when debt_service=0
        # under 100% LTV) and skip rows where any required metric is
        # non-finite. The serializer rejects nan/inf hard.
        import math
        def _safe(x, default=0.0):
            if x is None: return default
            try:
                xf = float(x)
                if math.isnan(xf) or math.isinf(xf):
                    return default
                return xf
            except (TypeError, ValueError):
                return default
        dscr_safe = min(999.0, _safe(dscr, 0.0))
        out.append(ZipReturn(
            zip=r.zip,
            state=r.state,
            cbsa_code=str(r.cbsa_code) if r.cbsa_code else None,
            cbsa_name=r.cbsa_name,
            typical_price=round(_safe(price)),
            typical_rent=round(_safe(rent)),
            appreciation_cagr=round(_safe(appr_cagr), 4),
            appreciation_cagr_5y_trail=round(_safe(cagr_5y), 4),
            appreciation_cagr_2y_trail=round(_safe(cagr_2y), 4),
            chg_12mo=round(_safe(chg_12mo), 4),
            appreciation_5y_pct=round(_safe(appr_5y_pct), 4),
            appreciation_5y_dollars=round(_safe(appr_5y_dollars), 2),
            rental_profit_5y=round(_safe(rental_profit_5y), 2),
            total_return_5y_dollars=round(_safe(total_5y), 2),
            total_return_5y_pct=round(_safe(total_5y / equity) if equity > 0 else 0.0, 4),
            irr_5y=round(_safe(irr_5y), 4),
            cap_rate_y1=round(_safe(cap_rate), 4),
            dscr_y1=round(dscr_safe, 2),
            vacancy_used=round(_safe(vacancy), 4),
            archetype_hint=archetype,
            redfin_search_url=f"https://www.redfin.com/zipcode/{r.zip}",
            zillow_search_url=f"https://www.zillow.com/homes/{r.zip}_rb/",
        ))

    sort_key = {
        "irr":          lambda z: z.irr_5y,
        "total_return": lambda z: z.total_return_5y_dollars,
        "cashflow":     lambda z: z.rental_profit_5y,
        "appreciation": lambda z: z.appreciation_5y_dollars,
        "yield":        lambda z: z.cap_rate_y1,
    }[sort]
    out.sort(key=sort_key, reverse=True)
    return out[:limit]


def to_dict(z: ZipReturn) -> dict:
    return asdict(z)
