"""Static reference datasets bundled with the package.

These are slow-moving / one-time datasets where a download URL is unstable
or paywalled but the values are publicly published. We embed canonical
values so the platform works out-of-the-box; users can swap richer
licensed data via the same tables.

Sources:
  - Saiz (2010, QJE Table 5) — housing supply elasticity for ~95 MSAs
  - Gyourko, Saiz, Summers (2018) — Wharton Residential Land Use
    Regulatory Index, by metro
  - Tax Foundation / Lincoln Institute (2024) — effective property tax
    rate by state
"""
from __future__ import annotations
import pandas as pd
import duckdb
from ..store import upsert_df

# Saiz (2010) supply elasticity, top metros. Lower = more supply-inelastic
# = stronger appreciation thesis. Values from Saiz (2010) Table 5.
SAIZ_ELASTICITY = [
    ("Miami, FL", 0.60, "FL"),
    ("Los Angeles-Long Beach, CA", 0.63, "CA"),
    ("Fort Lauderdale, FL", 0.65, "FL"),
    ("San Francisco, CA", 0.66, "CA"),
    ("San Diego, CA", 0.67, "CA"),
    ("Oakland, CA", 0.70, "CA"),
    ("Salt Lake City, UT", 0.75, "UT"),
    ("Ventura, CA", 0.75, "CA"),
    ("New Orleans, LA", 0.81, "LA"),
    ("Honolulu, HI", 0.82, "HI"),
    ("New York, NY", 0.76, "NY"),
    ("Boston, MA", 0.86, "MA"),
    ("Newark, NJ", 1.16, "NJ"),
    ("Seattle, WA", 0.88, "WA"),
    ("Riverside-San Bernardino, CA", 0.94, "CA"),
    ("Sacramento, CA", 1.32, "CA"),
    ("Tampa, FL", 1.00, "FL"),
    ("Minneapolis-St. Paul, MN", 1.45, "MN"),
    ("Washington, DC", 1.61, "DC"),
    ("Phoenix, AZ", 1.61, "AZ"),
    ("Denver, CO", 1.53, "CO"),
    ("Las Vegas, NV", 1.39, "NV"),
    ("Pittsburgh, PA", 1.20, "PA"),
    ("Chicago, IL", 1.49, "IL"),
    ("Charlotte, NC", 3.09, "NC"),
    ("Atlanta, GA", 2.55, "GA"),
    ("Memphis, TN", 2.10, "TN"),
    ("Nashville, TN", 2.24, "TN"),
    ("Orlando, FL", 1.12, "FL"),
    ("Houston, TX", 2.30, "TX"),
    ("Dallas, TX", 2.18, "TX"),
    ("Austin, TX", 3.00, "TX"),
    ("San Antonio, TX", 2.98, "TX"),
    ("Indianapolis, IN", 4.00, "IN"),
    ("Kansas City, MO", 3.19, "MO"),
    ("Cleveland, OH", 1.02, "OH"),
    ("Cincinnati, OH", 2.21, "OH"),
    ("Columbus, OH", 2.71, "OH"),
    ("Detroit, MI", 1.24, "MI"),
    ("St. Louis, MO", 2.36, "MO"),
    ("Birmingham, AL", 2.18, "AL"),
    ("Oklahoma City, OK", 3.29, "OK"),
    ("Raleigh-Durham, NC", 2.11, "NC"),
    ("Jacksonville, FL", 1.65, "FL"),
    ("Boise, ID", 1.60, "ID"),
    ("Portland, OR", 1.07, "OR"),
    ("Baltimore, MD", 1.23, "MD"),
    ("Philadelphia, PA", 1.65, "PA"),
    ("Milwaukee, WI", 1.03, "WI"),
]

# Gyourko/Saiz/Summers Wharton Residential Land Use Regulatory Index 2018.
# Higher = more regulatory friction = thicker affordability wedge.
# Subset of major metros, standardized z-scores from the 2018 update.
WRLURI_2018 = [
    ("San Francisco, CA", 1.69, "CA"),
    ("New York, NY", 1.82, "NY"),
    ("Boston, MA", 1.62, "MA"),
    ("Los Angeles-Long Beach, CA", 1.07, "CA"),
    ("San Diego, CA", 1.10, "CA"),
    ("Seattle, WA", 1.00, "WA"),
    ("Washington, DC", 0.86, "DC"),
    ("Honolulu, HI", 1.20, "HI"),
    ("Denver, CO", 0.55, "CO"),
    ("Portland, OR", 0.74, "OR"),
    ("Miami, FL", 0.41, "FL"),
    ("Sacramento, CA", 0.74, "CA"),
    ("Riverside-San Bernardino, CA", 0.40, "CA"),
    ("Chicago, IL", 0.18, "IL"),
    ("Philadelphia, PA", 0.05, "PA"),
    ("Baltimore, MD", 0.20, "MD"),
    ("Pittsburgh, PA", -0.03, "PA"),
    ("Phoenix, AZ", -0.20, "AZ"),
    ("Tampa, FL", -0.18, "FL"),
    ("Orlando, FL", -0.20, "FL"),
    ("Las Vegas, NV", -0.32, "NV"),
    ("Salt Lake City, UT", -0.05, "UT"),
    ("Atlanta, GA", -0.27, "GA"),
    ("Charlotte, NC", -0.29, "NC"),
    ("Raleigh-Durham, NC", -0.21, "NC"),
    ("Nashville, TN", -0.43, "TN"),
    ("Memphis, TN", -0.65, "TN"),
    ("Birmingham, AL", -0.71, "AL"),
    ("Kansas City, MO", -0.71, "MO"),
    ("St. Louis, MO", -0.45, "MO"),
    ("Indianapolis, IN", -0.75, "IN"),
    ("Cleveland, OH", -0.41, "OH"),
    ("Cincinnati, OH", -0.45, "OH"),
    ("Columbus, OH", -0.50, "OH"),
    ("Detroit, MI", -0.49, "MI"),
    ("Houston, TX", -0.65, "TX"),
    ("Dallas, TX", -0.45, "TX"),
    ("Austin, TX", -0.20, "TX"),
    ("San Antonio, TX", -0.65, "TX"),
    ("Oklahoma City, OK", -0.75, "OK"),
    ("Jacksonville, FL", -0.30, "FL"),
    ("New Orleans, LA", -0.40, "LA"),
    ("Milwaukee, WI", -0.20, "WI"),
    ("Minneapolis-St. Paul, MN", 0.10, "MN"),
    ("Boise, ID", -0.30, "ID"),
]

# Effective property tax rate by state, 2024 (Tax Foundation / ATTOM).
# Pct of home value. NJ highest, HI lowest.
PROPERTY_TAX_RATE = [
    ("AL", 0.40), ("AK", 1.07), ("AZ", 0.51), ("AR", 0.62), ("CA", 0.71),
    ("CO", 0.49), ("CT", 1.79), ("DE", 0.59), ("DC", 0.57), ("FL", 0.74),
    ("GA", 0.81), ("HI", 0.27), ("ID", 0.52), ("IL", 1.95), ("IN", 0.81),
    ("IA", 1.43), ("KS", 1.26), ("KY", 0.76), ("LA", 0.51), ("ME", 1.09),
    ("MD", 0.99), ("MA", 1.04), ("MI", 1.24), ("MN", 1.02), ("MS", 0.65),
    ("MO", 0.91), ("MT", 0.74), ("NE", 1.54), ("NV", 0.44), ("NH", 1.61),
    ("NJ", 2.08), ("NM", 0.59), ("NY", 1.40), ("NC", 0.63), ("ND", 0.88),
    ("OH", 1.30), ("OK", 0.76), ("OR", 0.77), ("PA", 1.36), ("RI", 1.30),
    ("SC", 0.46), ("SD", 1.01), ("TN", 0.48), ("TX", 1.47), ("UT", 0.47),
    ("VT", 1.71), ("VA", 0.72), ("WA", 0.76), ("WV", 0.49), ("WI", 1.51),
    ("WY", 0.55),
]


def load(con: duckdb.DuckDBPyConnection) -> int:
    n = 0
    n += upsert_df(
        con, "saiz_elasticity",
        pd.DataFrame(SAIZ_ELASTICITY, columns=["cbsa_name", "elasticity", "state"]),
    )
    n += upsert_df(
        con, "wharton_wrluri",
        pd.DataFrame(WRLURI_2018, columns=["cbsa_name", "wrluri_2018", "state"]),
    )
    n += upsert_df(
        con, "property_tax_state",
        pd.DataFrame(PROPERTY_TAX_RATE, columns=["state", "effective_rate_pct"]),
    )
    return n
