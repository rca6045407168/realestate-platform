"""Zip-level AVM mispricing signal (Framework §5.7 — Information alpha).

The insight: Zillow ZHVI is a smoothed, slow-moving index of typical home
values. Redfin median_sale_price is a fast-moving readout of what's
actually transacting. When they diverge, the market is repricing in a
direction the index hasn't caught up to.

Definition:
  divergence_pct = (recent Redfin median sale - latest ZHVI) / ZHVI

Interpretation:
  +1σ or more  = 'hot'      — sales clearing above the smoothed value;
                              probably appreciating faster than the index
                              shows. Tailwind for resale; danger for buy.
  -1σ or more  = 'cold'     — sales clearing below the smoothed value;
                              market softening but index lags. Buying
                              opportunity OR a flag that the comp set
                              doesn't apply (small samples).
  within ±1σ   = 'aligned'  — nothing to see.

This is the Information layer of the framework: a custom signal that's
cheap to compute but absent from off-the-shelf tools.
"""
from __future__ import annotations
import pandas as pd
import duckdb
from .store import upsert_df

SQL = """
WITH zhvi_now AS (
    SELECT zip, value AS zhvi
    FROM (
        SELECT zip, value, ROW_NUMBER() OVER (PARTITION BY zip ORDER BY period DESC) AS rn
        FROM zillow_zhvi
    ) WHERE rn = 1
),
redfin_recent AS (
    -- Anchor to Redfin's most recent period (their data lags 1–3 months)
    -- and take a 90-day window. Weight median_sale_price by homes_sold so
    -- low-volume zips with one luxury sale don't dominate, and require
    -- minimum transaction depth for inclusion.
    SELECT geo_id AS zip,
           SUM(median_sale_price * COALESCE(homes_sold, 1))
             / NULLIF(SUM(COALESCE(homes_sold, 1)), 0) AS redfin_sale_90d,
           SUM(COALESCE(homes_sold, 0)) AS sales_count
    FROM redfin_market
    WHERE geo_type = 'zip'
      AND median_sale_price IS NOT NULL
      AND period >= (
          (SELECT MAX(period) FROM redfin_market WHERE geo_type='zip')
          - INTERVAL '90 days'
      )
    GROUP BY geo_id
    HAVING SUM(COALESCE(homes_sold, 0)) >= 20  -- sample-size floor
)
SELECT z.zip, z.zhvi, r.redfin_sale_90d,
       (r.redfin_sale_90d - z.zhvi) / NULLIF(z.zhvi, 0) AS divergence_pct
FROM zhvi_now z JOIN redfin_recent r USING (zip)
WHERE z.zhvi IS NOT NULL AND r.redfin_sale_90d IS NOT NULL
  -- Drop wildly implausible divergences (>3x or <0.3x of index = bad data)
  AND r.redfin_sale_90d BETWEEN z.zhvi * 0.4 AND z.zhvi * 2.5
"""


def compute(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute(SQL).df()
    if df.empty:
        return df
    # Robust z = (x - median) / IQR, gentler tails than mean/sd.
    med = df["divergence_pct"].median()
    iqr = df["divergence_pct"].quantile(0.75) - df["divergence_pct"].quantile(0.25)
    df["divergence_z"] = (df["divergence_pct"] - med) / iqr if iqr else 0.0
    df["direction"] = "aligned"
    df.loc[df["divergence_z"] >= 1.0, "direction"] = "hot"
    df.loc[df["divergence_z"] <= -1.0, "direction"] = "cold"
    return df[["zip", "zhvi", "redfin_sale_90d", "divergence_pct", "divergence_z", "direction"]]


def persist(con: duckdb.DuckDBPyConnection) -> int:
    df = compute(con)
    if df.empty:
        return 0
    con.execute("DELETE FROM zip_avm_signal")
    return upsert_df(con, "zip_avm_signal", df)
