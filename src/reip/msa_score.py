"""MSA scoring per the framework's Table 5.

Two outputs per CBSA:
  - APPRECIATION SCORE: weighted toward demand + supply
  - CASHFLOW SCORE:     weighted toward yield + risk
  - TOTAL RETURN SCORE: blend (default 50/50)

Factor weights from the framework:

  Demand (40%):
    - 5-yr population CAGR              10%   (ACS)
    - 5-yr employment CAGR              10%   (BLS QCEW)
    - 5-yr median household income CAGR 10%   (ACS)
    - Net domestic migration % of pop   10%   (IRS)

  Supply (20%):
    - Permits per 1,000 households (3yr) 10%  (Census BPS)
    - Months of inventory                 5%  (Redfin)
    - Saiz elasticity (inverted)          5%  (Saiz / Wharton WRLURI)

  Pricing/Yield (20%):
    - Gross rent yield                   10%  (ZORI / ZHVI)
    - Price-to-income ratio (inverted)    5%  (ZHVI / ACS income)
    - 12-month DOM trend (inverted)       5%  (Redfin)

  Risk (20%):
    - Climate risk score (FEMA proxy)     5%
    - Insurance/disaster trend            5%  (FEMA paid trend proxy)
    - Regulatory friction (WRLURI)        5%
    - Effective property tax rate         5%

The Appreciation Score weights Demand+Supply heavily. The Cashflow Score
weights Yield + (low) Risk heavily. Per the paper, NO trailing price
appreciation is used — only leading indicators.
"""
from __future__ import annotations
import duckdb
import numpy as np
import pandas as pd
from .store import connect


SQL = """
WITH zhvi_now AS (
    SELECT zip, value AS zhvi
    FROM (
        SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
        FROM zillow_zhvi
    ) WHERE rn = 1
),
zori_now AS (
    SELECT zip, value AS zori
    FROM (
        SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
        FROM zillow_zori
    ) WHERE rn = 1
),
zip_to_cbsa AS (
    SELECT z.zip, c.cbsa_code, c.cbsa_name, c.state
    FROM zip_county_xwalk z
    JOIN county_cbsa_xwalk c USING (fips_county)
),
zhvi_msa AS (
    SELECT cbsa_code, MEDIAN(zhvi) AS zhvi_med
    FROM zhvi_now z JOIN zip_to_cbsa zc USING (zip)
    GROUP BY cbsa_code
),
zori_msa AS (
    SELECT cbsa_code, MEDIAN(zori) AS zori_med
    FROM zori_now z JOIN zip_to_cbsa zc USING (zip)
    GROUP BY cbsa_code
),
redfin_msa AS (
    SELECT cbsa_code,
           AVG(median_days_on_market) AS dom_avg,
           AVG(inventory) AS inv_avg
    FROM redfin_market r
    JOIN zip_to_cbsa zc ON zc.zip = r.geo_id AND r.geo_type = 'zip'
    WHERE r.period >= (CURRENT_DATE - INTERVAL '90 days')
    GROUP BY cbsa_code
),
redfin_msa_yr AS (
    SELECT cbsa_code,
           AVG(median_days_on_market) AS dom_avg_yr_ago
    FROM redfin_market r
    JOIN zip_to_cbsa zc ON zc.zip = r.geo_id AND r.geo_type = 'zip'
    WHERE r.period BETWEEN (CURRENT_DATE - INTERVAL '15 months') AND (CURRENT_DATE - INTERVAL '12 months')
    GROUP BY cbsa_code
),
acs_now AS (
    SELECT fips_county, year, population, households,
           median_household_income AS hh_income
    FROM acs_county WHERE year = (SELECT MAX(year) FROM acs_county)
),
acs_then AS (
    SELECT fips_county, year, population, households,
           median_household_income AS hh_income
    FROM acs_county WHERE year = (SELECT MIN(year) FROM acs_county)
),
acs_msa_now AS (
    SELECT c.cbsa_code, SUM(a.population) AS pop, SUM(a.households) AS hh,
           SUM(a.population * a.hh_income) / NULLIF(SUM(a.population), 0) AS hh_income
    FROM acs_now a JOIN county_cbsa_xwalk c USING (fips_county) GROUP BY c.cbsa_code
),
acs_msa_then AS (
    SELECT c.cbsa_code, SUM(a.population) AS pop_then, SUM(a.households) AS hh_then,
           SUM(a.population * a.hh_income) / NULLIF(SUM(a.population), 0) AS hh_income_then
    FROM acs_then a JOIN county_cbsa_xwalk c USING (fips_county) GROUP BY c.cbsa_code
),
permits_msa AS (
    SELECT c.cbsa_code, SUM(p.units_total) AS permits_3yr
    FROM census_permits p
    JOIN county_cbsa_xwalk c USING (fips_county)
    WHERE p.period >= (CURRENT_DATE - INTERVAL '36 months')
    GROUP BY c.cbsa_code
),
migration_msa AS (
    -- IRS migration data includes pseudo-codes for Foreign (96xxx), region
    -- aggregates (97xxx), and "different state" rollups (59xxx). We require
    -- both endpoints to be real US counties present in our crosswalk, and
    -- only count flows that cross the destination MSA boundary.
    SELECT c.cbsa_code,
           SUM(CASE WHEN m.direction='inflow'  THEN m.exemptions ELSE 0 END) AS in_persons,
           SUM(CASE WHEN m.direction='outflow' THEN m.exemptions ELSE 0 END) AS out_persons
    FROM irs_migration m
    JOIN county_cbsa_xwalk c  ON c.fips_county  = m.fips_county
    JOIN county_cbsa_xwalk c2 ON c2.fips_county = m.counterparty_fips
    WHERE c.cbsa_code != c2.cbsa_code
    GROUP BY c.cbsa_code
),
employ_msa AS (
    SELECT c.cbsa_code,
           SUM(CASE WHEN q.period = (SELECT MAX(period) FROM bls_qcew) THEN q.employment ELSE 0 END) AS emp_now,
           SUM(CASE WHEN q.period = (SELECT MIN(period) FROM bls_qcew) THEN q.employment ELSE 0 END) AS emp_then
    FROM bls_qcew q
    JOIN county_cbsa_xwalk c USING (fips_county)
    WHERE q.industry_code = '10'
    GROUP BY c.cbsa_code
),
fema_msa AS (
    SELECT c.cbsa_code, SUM(f.claim_count) AS flood_claims, SUM(f.total_paid) AS flood_paid
    FROM fema_nfip f
    JOIN county_cbsa_xwalk c USING (fips_county)
    WHERE f.year >= (EXTRACT(YEAR FROM CURRENT_DATE) - 10)
    GROUP BY c.cbsa_code
),
saiz_msa AS (
    SELECT c.cbsa_code, AVG(s.elasticity) AS elasticity
    FROM county_cbsa_xwalk c
    LEFT JOIN saiz_elasticity s ON LOWER(c.cbsa_name) LIKE '%' || LOWER(SPLIT_PART(s.cbsa_name, ',', 1)) || '%'
    GROUP BY c.cbsa_code
),
wrluri_msa AS (
    SELECT c.cbsa_code, AVG(w.wrluri_2018) AS wrluri
    FROM county_cbsa_xwalk c
    LEFT JOIN wharton_wrluri w ON LOWER(c.cbsa_name) LIKE '%' || LOWER(SPLIT_PART(w.cbsa_name, ',', 1)) || '%'
    GROUP BY c.cbsa_code
),
prop_tax_msa AS (
    SELECT c.cbsa_code, AVG(t.effective_rate_pct) AS prop_tax_pct
    FROM county_cbsa_xwalk c
    LEFT JOIN property_tax_state t ON UPPER(SUBSTR(TRIM(SPLIT_PART(c.cbsa_name, ',', 2)), 1, 2)) = t.state
    GROUP BY c.cbsa_code
)
SELECT
    cx.cbsa_code, ANY_VALUE(cx.cbsa_name) AS cbsa_name,
    ANY_VALUE(cx.cbsa_type) AS cbsa_type,
    zh.zhvi_med, zo.zori_med,
    rd.dom_avg, rd.inv_avg, rdy.dom_avg_yr_ago,
    an.pop, an.hh, an.hh_income,
    at_acs.pop_then, at_acs.hh_then, at_acs.hh_income_then,
    pm.permits_3yr,
    mig.in_persons, mig.out_persons,
    em.emp_now, em.emp_then,
    fm.flood_claims, fm.flood_paid,
    sz.elasticity,
    wr.wrluri,
    pt.prop_tax_pct
FROM county_cbsa_xwalk cx
LEFT JOIN zhvi_msa zh USING (cbsa_code)
LEFT JOIN zori_msa zo USING (cbsa_code)
LEFT JOIN redfin_msa rd USING (cbsa_code)
LEFT JOIN redfin_msa_yr rdy USING (cbsa_code)
LEFT JOIN acs_msa_now an USING (cbsa_code)
LEFT JOIN acs_msa_then at_acs USING (cbsa_code)
LEFT JOIN permits_msa pm USING (cbsa_code)
LEFT JOIN migration_msa mig USING (cbsa_code)
LEFT JOIN employ_msa em USING (cbsa_code)
LEFT JOIN fema_msa fm USING (cbsa_code)
LEFT JOIN saiz_msa sz USING (cbsa_code)
LEFT JOIN wrluri_msa wr USING (cbsa_code)
LEFT JOIN prop_tax_msa pt USING (cbsa_code)
GROUP BY cx.cbsa_code, zh.zhvi_med, zo.zori_med, rd.dom_avg, rd.inv_avg, rdy.dom_avg_yr_ago,
         an.pop, an.hh, an.hh_income, at_acs.pop_then, at_acs.hh_then, at_acs.hh_income_then,
         pm.permits_3yr, mig.in_persons, mig.out_persons, em.emp_now, em.emp_then,
         fm.flood_claims, fm.flood_paid, sz.elasticity, wr.wrluri, pt.prop_tax_pct
"""


def features(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    own = False
    if con is None:
        con = connect(); own = True
    try:
        df = con.execute(SQL).df()
    finally:
        if own:
            con.close()
    return df


def _rz(s: pd.Series) -> pd.Series:
    """Robust z-score: (x − median) / IQR."""
    med = s.median()
    iqr = s.quantile(0.75) - s.quantile(0.25)
    if iqr == 0 or pd.isna(iqr):
        return s * 0
    return (s - med) / iqr


def derive_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the 14 framework factors from raw MSA aggregates."""
    out = df.copy()
    # 5-yr CAGRs (ACS years are 2018→2023, so 5-yr basis assumed)
    yrs = 5
    out["pop_cagr_5yr"] = (out["pop"] / out["pop_then"]) ** (1 / yrs) - 1
    out["income_cagr_5yr"] = (out["hh_income"] / out["hh_income_then"]) ** (1 / yrs) - 1
    out["emp_cagr_5yr"] = (out["emp_now"] / out["emp_then"]) ** (1 / yrs) - 1
    # Net migration as % of pop (using IRS exemptions ≈ persons)
    out["net_migration_pct_pop"] = (out["in_persons"] - out["out_persons"]) / out["pop"].clip(lower=1)
    # Supply
    out["permits_per_1000_hh"] = out["permits_3yr"] / (out["hh"] / 1000).clip(lower=0.001) / 3.0
    out["months_of_inventory"] = out["inv_avg"]  # already a months-equivalent in Redfin
    out["elasticity_inv"] = -out["elasticity"]   # higher = more inelastic = appreciation thesis
    # Yield
    out["gross_yield"] = (out["zori_med"] * 12) / out["zhvi_med"]
    out["price_to_income_inv"] = -(out["zhvi_med"] / out["hh_income"])
    out["dom_trend_inv"] = -(out["dom_avg"] - out["dom_avg_yr_ago"])
    # Risk: flood / household, insurance proxied as recent flood paid growth
    out["flood_per_hh"] = out["flood_claims"] / out["hh"].clip(lower=1)
    out["insurance_proxy"] = out["flood_paid"] / out["hh"].clip(lower=1)
    out["regulatory_friction"] = out["wrluri"]
    out["effective_property_tax"] = out["prop_tax_pct"]
    return out


# Per-factor weights from Table 5
WEIGHTS = {
    # Demand 40%
    "pop_cagr_5yr":           ("appreciation", +0.10),
    "emp_cagr_5yr":           ("appreciation", +0.10),
    "income_cagr_5yr":        ("appreciation", +0.10),
    "net_migration_pct_pop":  ("appreciation", +0.10),
    # Supply 20%
    "permits_per_1000_hh":    ("appreciation", -0.10),   # high permits = oversupply = bad for appr
    "months_of_inventory":    ("appreciation", -0.05),
    "elasticity_inv":         ("appreciation", +0.05),
    # Yield 20% — drives the cashflow score
    "gross_yield":            ("cashflow",     +0.10),
    "price_to_income_inv":    ("cashflow",     +0.05),
    "dom_trend_inv":          ("cashflow",     +0.05),
    # Risk 20% — penalizes both scores
    "flood_per_hh":           ("risk",         +0.05),
    "insurance_proxy":        ("risk",         +0.05),
    "regulatory_friction":    ("risk",         +0.05),
    "effective_property_tax": ("risk",         +0.05),
}


def score(df: pd.DataFrame, blend_w_appr: float = 0.5) -> pd.DataFrame:
    out = derive_factors(df)
    appr = pd.Series(0.0, index=out.index)
    cash = pd.Series(0.0, index=out.index)
    risk = pd.Series(0.0, index=out.index)
    completeness = pd.Series(0, index=out.index)
    for col, (group, w) in WEIGHTS.items():
        if col not in out.columns:
            continue
        z = _rz(out[col]).fillna(0)
        present = out[col].notna().astype(int)
        completeness = completeness + present
        if group == "appreciation":
            appr = appr + w * z
        elif group == "cashflow":
            cash = cash + w * z
        else:
            risk = risk + w * z
    out["appreciation_score"] = appr - 0.5 * risk
    out["cashflow_score"]     = cash - 0.5 * risk
    out["total_return_score"] = blend_w_appr * out["appreciation_score"] + (1 - blend_w_appr) * out["cashflow_score"]
    out["completeness"] = completeness / len(WEIGHTS)
    # Filter to MSAs only (drop μSA noise) for the headline ranking
    out = out[out["pop"].notna() & (out["pop"] >= 50_000)]
    return out.sort_values("total_return_score", ascending=False)


# --- Archetype classifier --------------------------------------------------

def classify_archetype(row: pd.Series) -> str:
    """Map an MSA to one of the five archetypes from §4 of the framework.

    Heuristic from Table 2:
      Coastal Gateway     — yield <= 4% AND elasticity_inv >= 0 (inelastic)
      Cashflow Heartland  — yield >= 7% AND pop_cagr <= 0.5%
      Sun Belt Growth     — pop_cagr >= 1.5% AND yield 5–7%
      Boom-Bust Beta      — std MSAs known by name + cyclical recovery
      Resource & Niche    — small MSAs / lifestyle / energy / college
    """
    yld = (row.get("gross_yield") or 0) * 100
    pop_cagr = (row.get("pop_cagr_5yr") or 0) * 100
    name = str(row.get("cbsa_name") or "").lower()
    pop = row.get("pop") or 0

    boom_bust = ("las vegas", "phoenix", "riverside", "cape coral", "reno")
    niche = ("boise", "bozeman", "bend", "midland", "williston", "branson", "sevierville",
             "joshua tree", "boulder", "fort collins")

    if any(k in name for k in boom_bust):
        return "Boom-Bust Beta"
    if any(k in name for k in niche):
        return "Resource & Niche"
    if pop < 250_000:
        return "Resource & Niche"
    if yld <= 4 and pop_cagr <= 1.5:
        return "Coastal Gateway"
    if yld >= 7 and pop_cagr <= 0.5:
        return "Cashflow Heartland"
    if 5 <= yld <= 7 and pop_cagr >= 1.5:
        return "Sun Belt Growth"
    return "Mixed"


def with_archetype(scored: pd.DataFrame) -> pd.DataFrame:
    out = scored.copy()
    out["archetype"] = out.apply(classify_archetype, axis=1)
    return out
