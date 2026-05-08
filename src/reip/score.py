"""Investment scoring.

Composite score per zip:
    score = w_yield * z(yield)
          + w_growth * z(growth)
          - w_risk * z(risk)

where:
    yield      = ZORI annualized rent / ZHVI median value (gross)
                 OR HUD FMR 2BR * 12 / ZHVI as fallback
    growth     = ZHVI 12-month % change
                 + IRS net AGI inflow / pop proxy
                 - permits / housing_stock proxy (oversupply penalty)
    risk       = FEMA NFIP claim count per capita
                 + Redfin median DOM (illiquidity)
                 - sale-to-list (seller power)

Each component is z-scored within the pool of zips with all data present.
"""
from __future__ import annotations
import duckdb
import pandas as pd
from .store import connect


SQL = """
WITH zhvi_latest AS (
    SELECT zip, period, value AS zhvi,
           ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
    FROM zillow_zhvi
),
zhvi_now AS (
    SELECT zip, zhvi FROM zhvi_latest WHERE rn = 1
),
zhvi_year_ago AS (
    SELECT z1.zip, z1.zhvi AS zhvi_yr_ago
    FROM zhvi_latest z1
    JOIN zhvi_now zn ON z1.zip = zn.zip
    WHERE z1.rn = 13
),
zori_now AS (
    SELECT zip, value AS zori
    FROM (
        SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
        FROM zillow_zori
    ) WHERE rn = 1
),
xwalk AS (
    SELECT zip, ANY_VALUE(fips_county) AS fips_county
    FROM zip_county_xwalk
    GROUP BY zip
),
redfin_zip AS (
    SELECT geo_id AS zip, period,
           median_days_on_market, sale_to_list, inventory,
           ROW_NUMBER() OVER (PARTITION BY geo_id ORDER BY period DESC) AS rn
    FROM redfin_market WHERE geo_type = 'zip'
),
redfin_now AS (
    SELECT zip, median_days_on_market AS dom, sale_to_list, inventory
    FROM redfin_zip WHERE rn = 1
),
permits_recent AS (
    SELECT fips_county, SUM(units_total) AS permits_12mo
    FROM census_permits
    WHERE period >= (CURRENT_DATE - INTERVAL '12 months')
    GROUP BY fips_county
),
migration AS (
    SELECT fips_county,
           SUM(CASE WHEN direction = 'inflow'  THEN agi_thousands ELSE 0 END) AS in_agi,
           SUM(CASE WHEN direction = 'outflow' THEN agi_thousands ELSE 0 END) AS out_agi
    FROM irs_migration
    GROUP BY fips_county
),
fema_recent AS (
    SELECT fips_county, SUM(claim_count) AS flood_claims_total, SUM(total_paid) AS flood_paid_total
    FROM fema_nfip
    WHERE year >= (EXTRACT(YEAR FROM CURRENT_DATE) - 10)
    GROUP BY fips_county
),
employ_latest AS (
    SELECT fips_county, period, employment, avg_weekly_wage,
           ROW_NUMBER() OVER (PARTITION BY fips_county ORDER BY period DESC) AS rn
    FROM bls_qcew WHERE industry_code = '10'
),
employ_now AS (
    SELECT fips_county, employment AS emp, avg_weekly_wage AS wage
    FROM employ_latest WHERE rn = 1
),
fmr AS (
    SELECT zip, fmr_2br
    FROM hud_fmr
    WHERE year = (SELECT MAX(year) FROM hud_fmr)
)
SELECT
    zn.zip,
    zn.zhvi,
    zo.zori,
    zya.zhvi_yr_ago,
    (zn.zhvi / NULLIF(zya.zhvi_yr_ago, 0) - 1) AS yoy_appreciation,
    -- gross yield: prefer ZORI, fall back to HUD FMR 2BR
    COALESCE(zo.zori * 12, fmr.fmr_2br * 12) / NULLIF(zn.zhvi, 0) AS gross_yield,
    rn.dom, rn.sale_to_list, rn.inventory,
    pr.permits_12mo,
    mi.in_agi, mi.out_agi,
    (mi.in_agi - mi.out_agi) AS net_agi_inflow,
    fr.flood_claims_total, fr.flood_paid_total,
    en.emp, en.wage
FROM zhvi_now zn
LEFT JOIN zori_now zo ON zo.zip = zn.zip
LEFT JOIN zhvi_year_ago zya ON zya.zip = zn.zip
LEFT JOIN redfin_now rn ON rn.zip = zn.zip
LEFT JOIN xwalk x ON x.zip = zn.zip
LEFT JOIN permits_recent pr ON pr.fips_county = x.fips_county
LEFT JOIN migration mi ON mi.fips_county = x.fips_county
LEFT JOIN fema_recent fr ON fr.fips_county = x.fips_county
LEFT JOIN employ_now en ON en.fips_county = x.fips_county
LEFT JOIN fmr ON fmr.zip = zn.zip
WHERE zn.zhvi IS NOT NULL
"""


def _z(s: pd.Series) -> pd.Series:
    return (s - s.mean()) / s.std(ddof=0)


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


def score(
    df: pd.DataFrame,
    w_yield: float = 0.4,
    w_growth: float = 0.4,
    w_risk: float = 0.2,
    yield_cap: float = 0.20,
    min_zhvi: float = 50_000,
    max_zhvi: float = 2_000_000,
) -> pd.DataFrame:
    out = df.copy()
    # Quality gates: drop zips with implausible price or yield (vacation-rental
    # ZORI noise, ghost-zip ZHVI). Yield > 20% gross is essentially always a
    # data artifact at the zip level.
    out = out[(out["zhvi"] >= min_zhvi) & (out["zhvi"] <= max_zhvi)].copy()
    out["gross_yield"] = out["gross_yield"].where(out["gross_yield"] <= yield_cap)

    # Use log() on dollar-denominated quantities so a few mega-counties don't
    # dominate the z-score. Robust z = (x - median) / IQR.
    import numpy as np

    def robust_z(s):
        med = s.median()
        iqr = s.quantile(0.75) - s.quantile(0.25)
        if iqr == 0 or pd.isna(iqr):
            return s * 0
        return (s - med) / iqr

    z_yield = robust_z(out["gross_yield"])
    z_appr = robust_z(out["yoy_appreciation"])
    z_migration = robust_z(np.sign(out["net_agi_inflow"]) * np.log1p(out["net_agi_inflow"].abs()))
    z_supply = robust_z(np.log1p(out["permits_12mo"].clip(lower=0)))   # higher = oversupply
    z_dom = robust_z(out["dom"])                                        # higher = slow
    z_flood = robust_z(np.log1p(out["flood_claims_total"].clip(lower=0)))  # higher = climate risk
    z_seller_power = robust_z(out["sale_to_list"])                      # higher = competitive

    growth = z_appr.fillna(0) + z_migration.fillna(0) - z_supply.fillna(0)
    risk = z_flood.fillna(0) + z_dom.fillna(0) - z_seller_power.fillna(0)

    out["score_yield"] = z_yield.fillna(0)
    out["score_growth"] = growth
    out["score_risk"] = risk
    out["score"] = (
        w_yield * out["score_yield"]
        + w_growth * out["score_growth"]
        - w_risk * out["score_risk"]
    )
    # Completeness: fraction of the 7 raw input signals present per zip.
    completeness_cols = ["gross_yield", "yoy_appreciation", "net_agi_inflow",
                         "permits_12mo", "dom", "flood_claims_total", "sale_to_list"]
    out["completeness"] = out[completeness_cols].notna().sum(axis=1) / len(completeness_cols)
    return out.sort_values("score", ascending=False)
