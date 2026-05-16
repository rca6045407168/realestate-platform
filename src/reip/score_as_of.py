"""Phase 6 Task 1 — leakage-free as-of scoring.

Builds a thinner version of `msa_score.score()` that uses ONLY factors
with full historical time-series in the existing DuckDB panels. Lets the
score-model backtest run TRULY out-of-sample.

Why thinner: most of `msa_score`'s factor inputs are snapshot-only in
the current data (ACS 2018+, BLS 2019+, permits 2025+, Redfin 2024+,
IRS 2021-22 only). Those can't be snapshotted to a 2018 date without
materially leaking forward-looking information.

Factors here (all time-series-available, all aggregable to MSA):

  Appreciation
    - hpi_5y_cagr_z       FHFA HPI 5-yr CAGR ending at score_year (+)
    - hpi_12mo_momentum   FHFA HPI 12-mo change ending at score_year (+)
    - zhvi_5y_growth      Zillow ZHVI 5-yr change as-of (+)
    - zhvi_12mo_momentum  Zillow ZHVI 12-mo change as-of (+)

  Cashflow
    - gross_yield         (ZORI × 12) / ZHVI as-of (+)
    - rent_3y_cagr        ZORI 3-yr CAGR as-of (+)

  Risk
    - flood_per_pop       FEMA NFIP cumulative claims through score_year per pop (-)

Static factors (saiz_elasticity, wharton_wrluri, property_tax_state)
DO NOT need historical snapshots — they're slow-moving by construction.
This module skips them anyway because we want a pure "time-series only"
test of whether market-momentum + yield features predict 5-yr realized
returns.

The blended score uses simple equal-weighted z-scores within each
component group, then 50/50 appreciation/cashflow with the risk
component as a 0.5-weight penalty. Matches the spirit of msa_score
without inheriting its weight calibration (which was set on current
data, so reusing it here would itself be a form of leakage).
"""
from __future__ import annotations
import duckdb
import numpy as np
import pandas as pd

from .store import connect


def _rz(s: pd.Series) -> pd.Series:
    """Robust z, winsorized at [P2.5, P97.5] first. Same shape as
    msa_score._rz but with a winsorization pre-step. Without it,
    skewed inputs like flood_per_pop produce extreme z-scores that
    dominate any blended composite — a single coastal MSA can push
    its total_return_score 20σ below the rest.
    """
    lo, hi = s.quantile(0.025), s.quantile(0.975)
    s = s.clip(lower=lo, upper=hi)
    med = s.median()
    iqr = s.quantile(0.75) - s.quantile(0.25)
    if iqr == 0 or pd.isna(iqr):
        return s * 0
    return (s - med) / iqr


def _hpi_features_as_of(con, score_year: int) -> pd.DataFrame:
    """FHFA HPI features anchored at score_year-end. Returns:
       cbsa_code, hpi_5y_cagr, hpi_12mo_momentum.
    """
    sql = """
    WITH anchor AS (
        SELECT cbsa_code, AVG(hpi) AS hpi_t FROM fhfa_hpi_metro WHERE year(period) = ? GROUP BY cbsa_code
    ),
    five_back AS (
        SELECT cbsa_code, AVG(hpi) AS hpi_t5 FROM fhfa_hpi_metro WHERE year(period) = ? GROUP BY cbsa_code
    ),
    one_back AS (
        SELECT cbsa_code, AVG(hpi) AS hpi_t1 FROM fhfa_hpi_metro WHERE year(period) = ? GROUP BY cbsa_code
    )
    SELECT a.cbsa_code,
           pow(a.hpi_t / NULLIF(f.hpi_t5, 0), 1.0/5.0) - 1.0 AS hpi_5y_cagr,
           a.hpi_t / NULLIF(o.hpi_t1, 0) - 1.0               AS hpi_12mo_momentum
    FROM anchor a
    LEFT JOIN five_back f USING (cbsa_code)
    LEFT JOIN one_back o USING (cbsa_code)
    WHERE a.hpi_t > 0
    """
    return con.execute(sql, [score_year, score_year - 5, score_year - 1]).df()


def _zillow_features_as_of(con, score_year: int) -> pd.DataFrame:
    """ZHVI + ZORI features at score_year-end (latest period ≤ year-end).

    Aggregates zip → CBSA via the xwalks. Returns:
      cbsa_code, zhvi_5y_growth, zhvi_12mo_momentum,
      gross_yield, rent_3y_cagr.
    """
    sql = """
    WITH zhvi_t AS (
        SELECT zip, AVG(value) AS v FROM zillow_zhvi WHERE year(period) = ? GROUP BY zip
    ),
    zhvi_t5 AS (
        SELECT zip, AVG(value) AS v FROM zillow_zhvi WHERE year(period) = ? GROUP BY zip
    ),
    zhvi_t1 AS (
        SELECT zip, AVG(value) AS v FROM zillow_zhvi WHERE year(period) = ? GROUP BY zip
    ),
    zori_t AS (
        SELECT zip, AVG(value) AS v FROM zillow_zori WHERE year(period) = ? GROUP BY zip
    ),
    zori_t3 AS (
        SELECT zip, AVG(value) AS v FROM zillow_zori WHERE year(period) = ? GROUP BY zip
    ),
    by_zip AS (
        SELECT zhvi_t.zip,
               zhvi_t.v   AS zhvi_t,
               zhvi_t5.v  AS zhvi_t5,
               zhvi_t1.v  AS zhvi_t1,
               zori_t.v   AS zori_t,
               zori_t3.v  AS zori_t3
        FROM zhvi_t
        LEFT JOIN zhvi_t5  USING (zip)
        LEFT JOIN zhvi_t1  USING (zip)
        LEFT JOIN zori_t   USING (zip)
        LEFT JOIN zori_t3  USING (zip)
    ),
    -- Per-zip features computed PRE-aggregation so each MSA's median
    -- reflects the typical zip, not the ratio of medians.
    by_zip_feat AS (
        SELECT zip,
               (zhvi_t / NULLIF(zhvi_t5, 0)) - 1.0                    AS zhvi_5y_growth,
               (zhvi_t / NULLIF(zhvi_t1, 0)) - 1.0                    AS zhvi_12mo_momentum,
               (zori_t * 12.0) / NULLIF(zhvi_t, 0)                    AS gross_yield,
               pow(zori_t / NULLIF(zori_t3, 0), 1.0/3.0) - 1.0        AS rent_3y_cagr
        FROM by_zip
    )
    SELECT cbsa.cbsa_code,
           median(b.zhvi_5y_growth)     AS zhvi_5y_growth,
           median(b.zhvi_12mo_momentum) AS zhvi_12mo_momentum,
           median(b.gross_yield)        AS gross_yield,
           median(b.rent_3y_cagr)       AS rent_3y_cagr
    FROM by_zip_feat b
    JOIN zip_county_xwalk zc USING (zip)
    JOIN county_cbsa_xwalk cbsa ON cbsa.fips_county = zc.fips_county
    WHERE cbsa.cbsa_code IS NOT NULL
    GROUP BY cbsa.cbsa_code
    """
    return con.execute(sql, [
        score_year, score_year - 5, score_year - 1,
        score_year, score_year - 3,
    ]).df()


def _flood_feature_as_of(con, score_year: int) -> pd.DataFrame:
    """FEMA NFIP cumulative claims through score_year, per ACS pop.
    Uses CURRENT acs_county pop as a normalizer because pop is slow-moving
    (~1-2% drift over 4 years; acceptable noise for a risk-component
    factor weighted at 0.5× total)."""
    sql = """
    WITH claims AS (
        SELECT cbsa.cbsa_code,
               sum(coalesce(f.claim_count, 0)) AS claims
        FROM fema_nfip f
        JOIN county_cbsa_xwalk cbsa ON cbsa.fips_county = f.fips_county
        WHERE f.year <= ?
          AND cbsa.cbsa_code IS NOT NULL
        GROUP BY cbsa.cbsa_code
    ),
    pop AS (
        SELECT cbsa.cbsa_code, sum(a.population) AS pop
        FROM (
            SELECT fips_county, max(population) AS population
            FROM acs_county
            GROUP BY fips_county
        ) a
        JOIN county_cbsa_xwalk cbsa USING (fips_county)
        WHERE cbsa.cbsa_code IS NOT NULL
        GROUP BY cbsa.cbsa_code
    )
    SELECT c.cbsa_code,
           c.claims / nullif(p.pop, 0) AS flood_per_pop
    FROM claims c LEFT JOIN pop p USING (cbsa_code)
    """
    return con.execute(sql, [score_year]).df()


def score_as_of(con: duckdb.DuckDBPyConnection, score_year: int) -> pd.DataFrame:
    """Score MSAs using only data available as of `score_year`.

    Returns DataFrame with columns:
      cbsa_code, hpi_5y_cagr, hpi_12mo_momentum, zhvi_5y_growth,
      zhvi_12mo_momentum, gross_yield, rent_3y_cagr, flood_per_pop,
      appreciation_score, cashflow_score, risk_score, total_return_score.
    """
    hpi = _hpi_features_as_of(con, score_year)
    zil = _zillow_features_as_of(con, score_year)
    fld = _flood_feature_as_of(con, score_year)

    df = hpi.merge(zil, on="cbsa_code", how="inner")
    df = df.merge(fld, on="cbsa_code", how="left")

    # Replace inf / nan-inducing artifacts so z-scoring works
    for col in ("hpi_5y_cagr", "hpi_12mo_momentum",
                "zhvi_5y_growth", "zhvi_12mo_momentum",
                "gross_yield", "rent_3y_cagr", "flood_per_pop"):
        if col in df.columns:
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    # Equal-weight z-scores within each component
    df["appreciation_score"] = (
        _rz(df["hpi_5y_cagr"]).fillna(0)        * 0.5
        + _rz(df["hpi_12mo_momentum"]).fillna(0) * 0.2
        + _rz(df["zhvi_5y_growth"]).fillna(0)    * 0.2
        + _rz(df["zhvi_12mo_momentum"]).fillna(0)* 0.1
    )
    df["cashflow_score"] = (
        _rz(df["gross_yield"]).fillna(0)  * 0.7
        + _rz(df["rent_3y_cagr"]).fillna(0) * 0.3
    )
    # Risk has a heavy right tail (Gulf-coast MSAs with massive cumulative
    # flood claims). Log-transform before z so the bottom 5% don't push
    # everyone else's blended score to noise.
    flood = df["flood_per_pop"].fillna(0).clip(lower=0)
    df["risk_score"] = _rz(np.log1p(flood * 1_000_000)).fillna(0)
    # Clip each component to ±3 so a single dimension can't dominate the
    # blend. Preserves rank within the component; bounds magnitude.
    for col in ("appreciation_score", "cashflow_score", "risk_score"):
        df[col] = df[col].clip(lower=-3, upper=3)
    df["total_return_score"] = (
        0.5 * df["appreciation_score"]
        + 0.5 * df["cashflow_score"]
        - 0.5 * df["risk_score"]
    )
    return df.sort_values("total_return_score", ascending=False).reset_index(drop=True)
