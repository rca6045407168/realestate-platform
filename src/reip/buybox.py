"""Per-zip buy-box derivation.

Translates a zip's macro data (ZHVI, ZORI, growth, regime) into a
property-level buy-box an investor can actually use:

  - Target price band: 80–110% of ZHVI (buy meaningfully under median,
    avoid overpaying for outliers)
  - Target rent band: 90–110% of ZORI for SFR (SFRs typically rent at
    or slightly above the all-rental-type median)
  - Target rehab band: scaled to price band (low-end = more rehab)
  - Target ARV (BRRRR refi): trend-projected ZHVI over a 12-mo horizon
  - Target cap rate: deal-grade threshold (≥7% target / ≥6% floor)
  - Notes: regime warnings, vacancy callouts, rehab assumptions

Plus a `typical_deal` — the median price/rent inputs you'd feed straight
into the stress test to see how the zip's TYPICAL property would
underwrite. This is what powers "one-click stress test from Top Zips."
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional

from . import projection as proj_mod
from . import climate as climate_mod
from .store import connect


# Tunables — keep them in one place so the gate stays bisectable.
PRICE_BAND_LOW_PCT  = 0.80     # buy 20% under ZHVI = "negotiated"
PRICE_BAND_HIGH_PCT = 1.10     # buy up to 10% over for an A-grade outlier
RENT_BAND_LOW_PCT   = 0.90     # SFR vs apartment skew
RENT_BAND_HIGH_PCT  = 1.10
REHAB_LIGHT_PCT     = 0.05     # 5% of price for cosmetic
REHAB_HEAVY_PCT     = 0.18     # 18% of price for value-add BRRRR
ARV_HORIZON_YEARS   = 1.0      # trend-projected ARV horizon
ARV_TREND_DECAY     = 0.50     # only extend half of 12mo growth (mean-revert)
DEFAULT_BEDS        = "3"
DEFAULT_BATHS       = "2"
DEFAULT_SQFT_LOW    = 1100
DEFAULT_SQFT_HIGH   = 1700


_US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}

def _to_state_code(s) -> Optional[str]:
    """Normalize 'Missouri' / 'MO' / 'mo' / None / NaN → 'MO' or None."""
    if s is None:
        return None
    # Handle pandas NaN / numpy NaN / non-string types (zip_returns can yield those)
    if not isinstance(s, str):
        try:
            import math
            if isinstance(s, float) and math.isnan(s):
                return None
        except Exception:
            pass
        try:
            s = str(s)
        except Exception:
            return None
    s = s.strip()
    if not s or s.lower() == "nan":
        return None
    if len(s) == 2:
        return s.upper()
    return _US_STATES.get(s) or _US_STATES.get(s.title())


@dataclass
class BuyBox:
    zip: str
    state: Optional[str]
    cbsa_code: Optional[str]
    cbsa_name: Optional[str]
    archetype_hint: Optional[str]
    # ---- bands ----
    target_price_low: float
    target_price_mid: float
    target_price_high: float
    target_rent_low: float
    target_rent_mid: float
    target_rent_high: float
    target_rehab_light: float
    target_rehab_heavy: float
    # ---- ARV ----
    arv_now: float                # value at zhvi today (no rehab uplift)
    arv_trend_12mo: float          # trend-based: ZHVI × (1 + 12mo_growth × decay)
    arv_method: str               # primary method label
    # ---- gate-targeted thresholds ----
    target_cap_rate: float
    floor_cap_rate: float
    # ---- a "typical deal" — feed straight into /api/stress ----
    typical_deal: dict
    # ---- context for the human ----
    regime_label: str
    regime_score: float
    vacancy_used: float
    # ---- climate exposure (full ClimateScore dict if scored, else None) ----
    climate: Optional[dict] = None
    # ---- sales-based ARV (Redfin Data Center) — None if redfin_market lacks data ----
    arv_sales_based: Optional[dict] = None
    # ---- parent-MSA historical stability (FHFA HPI 1985-now) ----
    msa_stability: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


_QUERY = """
WITH zhvi_ranked AS (
    SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
    FROM zillow_zhvi
),
zhvi_now AS (SELECT zip, value AS zhvi FROM zhvi_ranked WHERE rn = 1),
zhvi_12  AS (SELECT zip, value AS zhvi_12mo FROM zhvi_ranked WHERE rn = 13),
zhvi_60  AS (SELECT zip, value AS zhvi_5y FROM zhvi_ranked WHERE rn = 60),
zori_ranked AS (
    SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
    FROM zillow_zori
),
zori_now AS (SELECT zip, value AS zori FROM zori_ranked WHERE rn = 1),
zori_12  AS (SELECT zip, value AS zori_12mo FROM zori_ranked WHERE rn = 13),
vacancy_per_zip AS (
    SELECT z.zip,
           a.vacant_for_rent / NULLIF(a.vacant_for_rent + a.renter_occupied, 0) AS vac
    FROM zip_county_xwalk z
    JOIN acs_county a ON a.fips_county = z.fips_county
    WHERE a.year = (SELECT MAX(year) FROM acs_county)
)
SELECT zhvi_now.zip, zhvi_now.zhvi,
       zhvi_12.zhvi_12mo, zhvi_60.zhvi_5y,
       zori_now.zori, zori_12.zori_12mo,
       vacancy_per_zip.vac,
       c.cbsa_code, c.cbsa_name, c.state
FROM zhvi_now
JOIN zori_now USING (zip)
LEFT JOIN zhvi_12 USING (zip)
LEFT JOIN zhvi_60 USING (zip)
LEFT JOIN zori_12 USING (zip)
LEFT JOIN vacancy_per_zip USING (zip)
LEFT JOIN zip_county_xwalk z ON z.zip = zhvi_now.zip
LEFT JOIN county_cbsa_xwalk c ON c.fips_county = z.fips_county
WHERE zhvi_now.zip = ?
"""


def _regime_from_growth(price_12mo: Optional[float], rent_12mo: Optional[float]) -> tuple[str, float]:
    """Same scheme as zip_returns: blended 12mo growth → regime label."""
    def _clip(x, lo=-0.15, hi=0.15):
        if x is None:
            return 0.0
        return max(lo, min(hi, x))
    score = (_clip(price_12mo) + _clip(rent_12mo)) / 2
    if score >= 0.05:
        label = "expanding"
    elif score >= 0.0:
        label = "mixed"
    elif score >= -0.05:
        label = "contracting"
    else:
        label = "crash"
    return label, round(score, 4)


def derive(con, zip_code: str, archetype_hint: Optional[str] = None) -> Optional[BuyBox]:
    """Return the buy box for a zip, or None if we have no data for it."""
    row = con.execute(_QUERY, [str(zip_code).zfill(5)]).fetchone()
    if not row:
        return None
    (z, zhvi, zhvi_12mo, zhvi_5y, zori, zori_12mo, vac,
     cbsa_code, cbsa_name, state_raw) = row
    state = _to_state_code(state_raw)

    zhvi = float(zhvi)
    zori = float(zori)
    price_12mo = (zhvi / float(zhvi_12mo) - 1) if zhvi_12mo else None
    rent_12mo  = (zori / float(zori_12mo) - 1) if zori_12mo else None
    regime_label, regime_score = _regime_from_growth(price_12mo, rent_12mo)
    vacancy_used = float(vac) if vac is not None else 0.06

    target_price_low  = round(zhvi * PRICE_BAND_LOW_PCT)
    target_price_mid  = round(zhvi)
    target_price_high = round(zhvi * PRICE_BAND_HIGH_PCT)
    target_rent_low   = round(zori * RENT_BAND_LOW_PCT)
    target_rent_mid   = round(zori)
    target_rent_high  = round(zori * RENT_BAND_HIGH_PCT)

    target_rehab_light = round(target_price_mid * REHAB_LIGHT_PCT)
    target_rehab_heavy = round(target_price_mid * REHAB_HEAVY_PCT)

    # Trend-based ARV. Half-decay the 12mo growth and apply over the horizon.
    growth_pa = (price_12mo or 0.0) * ARV_TREND_DECAY
    # Apply over horizon, but cap absolute growth at ±10% so we don't promise
    # a 60% appreciation in a hot market that's about to cool.
    growth_capped = max(-0.10, min(0.10, growth_pa)) * ARV_HORIZON_YEARS
    arv_trend = round(zhvi * (1 + growth_capped))

    # Gate-targeted thresholds. 7% target / 6% floor on a 75% LTV deal
    # roughly aligns with the stress-test GREEN gate.
    target_cap_rate = 0.07
    floor_cap_rate  = 0.06

    # Look up the real state-level property tax rate. Falls back to 1.2%
    # if state is unknown or the table is empty. (property_tax_state was
    # already loaded but the buy-box was hardcoded to 1.2% — fixed now.)
    state_tax_rate = 0.012
    if state:
        r = con.execute(
            "SELECT effective_rate_pct FROM property_tax_state WHERE state = ? LIMIT 1",
            [state],
        ).fetchone()
        if r and r[0] is not None:
            state_tax_rate = round(float(r[0]) / 100.0, 4)

    # Score the climate exposure for this zip
    climate_score = climate_mod.score_zip(con, str(z).zfill(5), state)

    # Climate-aware insurance baseline: bump default 1.2%-of-price by the
    # severity. A severe-climate zip should not be underwritten at the
    # national-average insurance line item.
    insurance_pct_of_price = 0.012
    if climate_score:
        if climate_score.category == "severe":
            insurance_pct_of_price = 0.025   # FL post-Ian-grade premium
        elif climate_score.category == "elevated":
            insurance_pct_of_price = 0.018
        elif climate_score.category == "moderate":
            insurance_pct_of_price = 0.014

    # Build the "typical deal" — feeds the stress test directly.
    typical_deal = {
        "purchase_price": target_price_mid,
        "monthly_rent":   target_rent_mid,
        "rehab_cost":     target_rehab_light,
        "vacancy":        round(max(0.05, vacancy_used), 3),
        "mortgage_rate":  0.07,
        "ltv":            0.75,
        "insurance_annual": round(target_price_mid * insurance_pct_of_price),
        "property_tax_rate": state_tax_rate,
        "state":          state,
    }

    notes = []
    if regime_label in ("contracting", "crash"):
        notes.append(f"Regime is {regime_label} — recent 12mo price/rent {regime_score*100:+.1f}%. "
                     f"Stress test will weigh worst case heavily; consider waiting or lowballing.")
    if regime_label == "expanding":
        notes.append(f"Regime is expanding — but ARV is capped at +10% to avoid extrapolating a hot streak.")
    if vacancy_used >= 0.10:
        notes.append(f"County vacancy {vacancy_used*100:.0f}% is elevated — base case uses {max(0.05, vacancy_used)*100:.0f}%, stress goes higher.")
    if zhvi < 60_000:
        notes.append(f"Sub-$60K zip — likely C/D class. Insurance, property mgmt, and tenant turn are usually under-modeled in this band.")
    notes.append(f"Target SFR {DEFAULT_BEDS}/{DEFAULT_BATHS}, {DEFAULT_SQFT_LOW}–{DEFAULT_SQFT_HIGH} sqft. "
                 f"Light rehab band assumes cosmetic; heavy assumes a value-add BRRRR.")

    # Climate notes — pulled into the buy-box note list so they surface
    # in chat answers and the UI buy-box modal.
    if climate_score:
        if climate_score.category in ("elevated", "severe"):
            notes.append(
                f"Climate risk: {climate_score.category.upper()} ({climate_score.overall_score}/100). "
                f"Primary risk: {climate_score.primary_risk}. "
                f"Insurance baseline adjusted to {insurance_pct_of_price*100:.1f}% of price (vs 1.2% inland)."
            )
        for n in climate_score.notes[:2]:
            notes.append(n)

    sales_based = arv_sales_based(con, str(z).zfill(5))

    # Pull parent-MSA stability tier so the buy box also shows the metro's
    # historical drawdown context (Boring / Standard / Volatile / Boom-Bust).
    try:
        from . import strategy as strategy_mod
        msa_stability = strategy_mod.stability_for(con, str(cbsa_code or ""))
    except Exception:
        msa_stability = None
    if msa_stability:
        # Surface as a note so it shows in chat answers too
        notes.append(
            f"Parent MSA ({cbsa_name or '?'}) historical stability: "
            f"{msa_stability['tier']} ({msa_stability['max_dd_pct']}% max drawdown 1985-now). "
            f"{'This metro was hit hard in 2007-2012.' if msa_stability['tier'] == 'Boom-Bust' else ''}"
            f"{'Shallow historical drawdowns — survives recessions well.' if msa_stability['tier'] == 'Boring' else ''}".strip()
        )

    return BuyBox(
        zip=str(z).zfill(5), state=state, cbsa_code=cbsa_code, cbsa_name=cbsa_name,
        archetype_hint=archetype_hint,
        target_price_low=target_price_low, target_price_mid=target_price_mid,
        target_price_high=target_price_high,
        target_rent_low=target_rent_low, target_rent_mid=target_rent_mid,
        target_rent_high=target_rent_high,
        target_rehab_light=target_rehab_light, target_rehab_heavy=target_rehab_heavy,
        arv_now=round(zhvi), arv_trend_12mo=arv_trend,
        arv_method="trend-based (ZHVI × half-decayed 12mo growth, capped ±10%)",
        arv_sales_based=sales_based,
        msa_stability=msa_stability,
        target_cap_rate=target_cap_rate, floor_cap_rate=floor_cap_rate,
        typical_deal=typical_deal,
        regime_label=regime_label, regime_score=regime_score,
        vacancy_used=round(vacancy_used, 4),
        climate=climate_mod.to_dict(climate_score) if climate_score else None,
        notes=notes,
    )


def to_dict(b: BuyBox) -> dict:
    return asdict(b)


# ---- ARV estimators ---------------------------------------------------------
#
# Two methods, used together for honesty:
#
#   1. trend-based (ZHVI × half-decayed 12mo growth, capped ±10%/yr) —
#      the model-based estimate. Smooth and projectable but Zillow-
#      smoothed median, not actual sales.
#
#   2. sales-based — uses the `redfin_market` table (loaded from
#      Redfin Data Center): trailing-3mo median_sale_price at the zip
#      level, plus 12mo trend, sale-to-list ratio, days on market.
#      This is the *actual transacted price* in the zip, not a smoothed
#      index. Closer to comp-based but uses zip medians, not
#      bed/bath-matched comps. Honest middle ground.
#
# Full bed/bath/sqft-matched comp-based ARV (BRRRR-grade) still needs
# either Redfin's live sold-comp endpoint (blocked from this network's
# IP — `gis-csv` returns Seattle regardless of `market` param) or a
# commercial feed (ATTOM, Black Knight). Documented in commit ab1abc6.

def arv_sales_based(con, zip_code: str, lookback_months: int = 3) -> Optional[dict]:
    """Sales-based ARV from Redfin Data Center zip-level history.

    Uses the trailing `lookback_months` of `redfin_market` rows to compute:
      - arv: median of median_sale_price across the window (more robust
        than a single month given thin samples in small zips)
      - sale_to_list: average of sale-to-list ratio (1.0 = at-list; >1
        = above-list bidding)
      - median_days_on_market: latest-month value
      - homes_sold_trailing: total sales in the window (sample-size
        confidence)
      - chg_12mo: vs. the same window 12 months back

    Returns None if there are zero sales in the lookback window.
    """
    zip5 = str(zip_code).zfill(5)
    rows = con.execute("""
        SELECT period, median_sale_price, median_list_price, homes_sold,
               sale_to_list, median_days_on_market
        FROM redfin_market
        WHERE geo_type='zip' AND geo_id = ?
        ORDER BY period DESC
    """, [zip5]).df()
    if rows.empty:
        return None
    recent = rows.head(lookback_months).dropna(subset=["median_sale_price"])
    if recent.empty:
        return None
    # 12mo-ago window for trend
    prior = rows.iloc[lookback_months + 9 : lookback_months + 9 + lookback_months]\
                 .dropna(subset=["median_sale_price"])

    import math
    arv = float(recent["median_sale_price"].median())
    chg_12mo = None
    if not prior.empty:
        prior_med = float(prior["median_sale_price"].median())
        if prior_med > 0:
            chg_12mo = round((arv / prior_med) - 1, 4)
    s2l = recent["sale_to_list"].dropna()
    avg_sale_to_list = float(s2l.mean()) if not s2l.empty else None
    if avg_sale_to_list is not None and math.isnan(avg_sale_to_list):
        avg_sale_to_list = None
    dom = recent["median_days_on_market"].dropna()
    median_dom = int(dom.iloc[0]) if not dom.empty else None
    homes_sold_trailing = int(recent["homes_sold"].fillna(0).sum())
    return {
        "method":               "sales-based (Redfin Data Center zip medians)",
        "arv":                  round(arv),
        "lookback_months":      lookback_months,
        "homes_sold_trailing":  homes_sold_trailing,
        "chg_12mo":             chg_12mo,
        "avg_sale_to_list":     round(avg_sale_to_list, 4) if avg_sale_to_list else None,
        "median_days_on_market": median_dom,
        "as_of":                str(recent["period"].iloc[0]).split("T")[0] if not recent.empty else None,
    }


def arv_estimate(con, zip_code: str,
                 horizons_years: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0)) -> Optional[dict]:
    """Project ARV for a zip across multiple horizons.

    Returns:
        {
          zip, state, cbsa_name,
          zhvi_now,
          price_12mo_growth,
          decayed_growth,
          method,
          horizons: [{years, projected_arv, basis: 'ZHVI', notes}],
          caveats: [str],
        }
    """
    row = con.execute(_QUERY, [str(zip_code).zfill(5)]).fetchone()
    if not row:
        return None
    (z, zhvi, zhvi_12mo, zhvi_5y, zori, zori_12mo, vac,
     cbsa_code, cbsa_name, state_raw) = row
    zhvi = float(zhvi)
    price_12mo = (zhvi / float(zhvi_12mo) - 1) if zhvi_12mo else 0.0
    decayed = max(-0.10, min(0.10, price_12mo * ARV_TREND_DECAY))

    horizons = []
    for years in horizons_years:
        projected = zhvi * (1 + decayed * years)
        notes = []
        if years == 0.0:
            notes.append("Today's ZHVI median — sale-now reference.")
        elif decayed > 0:
            notes.append(f"Half-decayed 12mo trend (+{decayed*100:.1f}%/yr) extrapolated {years:.1f}yr, capped at ±10%/yr.")
        elif decayed < 0:
            notes.append(f"Half-decayed 12mo trend ({decayed*100:.1f}%/yr) — market is softening; ARV may erode.")
        else:
            notes.append("Flat 12mo trend; ARV held constant.")
        horizons.append({
            "years": years,
            "projected_arv": round(projected),
            "basis": "ZHVI",
            "notes": " ".join(notes),
        })

    caveats = [
        "Trend-based estimate from Zillow ZHVI medians. Use for back-of-envelope only.",
        "Sales-based ARV (when available) uses actual Redfin transactions — trust that more "
        "than trend-based for refi-window decisions.",
        "Bed/bath/sqft-matched comp ARV (full BRRRR-grade) still requires a residential proxy or "
        "commercial feed (ATTOM/Black Knight).",
        "If the market is contracting (12mo < 0%), trend-extending into a refi window 6–12 months "
        "out is itself risky — your lender may appraise below your projection.",
    ]
    out = {
        "zip": str(z).zfill(5),
        "state": _to_state_code(state_raw),
        "cbsa_code": cbsa_code, "cbsa_name": cbsa_name,
        "zhvi_now": round(zhvi),
        "price_12mo_growth": round(price_12mo, 4),
        "decayed_growth_pa": round(decayed, 4),
        "method": "trend-based (ZHVI × half-decayed 12mo growth, capped ±10%/yr)",
        "horizons": horizons,
        "caveats": caveats,
    }
    # Add sales-based ARV when we have Redfin data for the zip
    sales = arv_sales_based(con, zip_code)
    if sales:
        out["sales_based"] = sales
    return out
