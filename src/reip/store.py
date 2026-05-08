"""DuckDB persistence layer.

Tables, all keyed by (geo_id, period) where geo_id is a 5-digit zip or 5-digit
FIPS county code. period is YYYY-MM for monthly series, YYYY for annual.

Each loader writes to its own raw table. The `features` view joins them into
one wide row per (zip, latest period) used by score.py.
"""
from __future__ import annotations
import duckdb
from pathlib import Path
from .config import DB_PATH


def connect(path: Path | str = DB_PATH) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute("INSTALL httpfs; LOAD httpfs;")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS zillow_zhvi (
    zip VARCHAR, period DATE, value DOUBLE,
    PRIMARY KEY (zip, period)
);
CREATE TABLE IF NOT EXISTS zillow_zori (
    zip VARCHAR, period DATE, value DOUBLE,
    PRIMARY KEY (zip, period)
);
CREATE TABLE IF NOT EXISTS redfin_market (
    geo_id VARCHAR, geo_type VARCHAR, period DATE,
    median_sale_price DOUBLE, median_list_price DOUBLE,
    homes_sold DOUBLE, new_listings DOUBLE, inventory DOUBLE,
    median_days_on_market DOUBLE, sale_to_list DOUBLE,
    pct_homes_sold_above_list DOUBLE, off_market_in_two_weeks DOUBLE,
    PRIMARY KEY (geo_id, geo_type, period)
);
CREATE TABLE IF NOT EXISTS irs_migration (
    period VARCHAR,                 -- e.g. 2021-2022
    direction VARCHAR,              -- 'inflow' or 'outflow'
    fips_county VARCHAR,            -- 5-digit FIPS of the county whose flows we measure
    counterparty_fips VARCHAR,      -- the other county
    returns DOUBLE, exemptions DOUBLE, agi_thousands DOUBLE,
    PRIMARY KEY (period, direction, fips_county, counterparty_fips)
);
CREATE TABLE IF NOT EXISTS census_permits (
    fips_county VARCHAR, period DATE,
    units_total DOUBLE, units_1unit DOUBLE, units_2unit DOUBLE,
    units_3to4 DOUBLE, units_5plus DOUBLE,
    PRIMARY KEY (fips_county, period)
);
CREATE TABLE IF NOT EXISTS bls_qcew (
    fips_county VARCHAR, period VARCHAR,   -- YYYY-Q
    industry_code VARCHAR,
    employment DOUBLE, total_wages DOUBLE, avg_weekly_wage DOUBLE,
    qtrly_estabs DOUBLE,
    PRIMARY KEY (fips_county, period, industry_code)
);
CREATE TABLE IF NOT EXISTS hud_fmr (
    zip VARCHAR, year INTEGER,
    fmr_0br DOUBLE, fmr_1br DOUBLE, fmr_2br DOUBLE, fmr_3br DOUBLE, fmr_4br DOUBLE,
    PRIMARY KEY (zip, year)
);
CREATE TABLE IF NOT EXISTS fema_nfip (
    fips_county VARCHAR, year INTEGER,
    claim_count DOUBLE, total_paid DOUBLE,
    PRIMARY KEY (fips_county, year)
);
CREATE TABLE IF NOT EXISTS fred_macro (
    series_id VARCHAR, period DATE, value DOUBLE,
    PRIMARY KEY (series_id, period)
);
CREATE TABLE IF NOT EXISTS zip_county_xwalk (
    zip VARCHAR, fips_county VARCHAR, weight DOUBLE,
    PRIMARY KEY (zip, fips_county)
);
CREATE TABLE IF NOT EXISTS zip_avm_signal (
    zip VARCHAR PRIMARY KEY,
    zhvi DOUBLE,                    -- Zillow's smoothed value index, latest
    redfin_sale_90d DOUBLE,         -- Avg Redfin median_sale_price, last 90d
    divergence_pct DOUBLE,          -- (redfin_sale - zhvi) / zhvi
    divergence_z DOUBLE,            -- z-score of divergence_pct across all zips
    direction VARCHAR,              -- 'hot' (sales > index) | 'cold' (sales < index) | 'aligned'
    refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_freshness (
    source_name VARCHAR PRIMARY KEY,
    last_refresh TIMESTAMP,
    rows_loaded INTEGER,
    expected_cadence_days INTEGER
);
CREATE TABLE IF NOT EXISTS msa_rank_snapshots (
    snapshot_at TIMESTAMP, cbsa_code VARCHAR,
    cbsa_name VARCHAR, archetype VARCHAR,
    appreciation_score DOUBLE, cashflow_score DOUBLE,
    total_return_score DOUBLE, total_rank INTEGER,
    appreciation_rank INTEGER, cashflow_rank INTEGER,
    PRIMARY KEY (snapshot_at, cbsa_code)
);
CREATE TABLE IF NOT EXISTS county_cbsa_xwalk (
    fips_county VARCHAR PRIMARY KEY,
    cbsa_code VARCHAR, cbsa_name VARCHAR, cbsa_type VARCHAR,
    state VARCHAR
);
CREATE TABLE IF NOT EXISTS acs_county (
    fips_county VARCHAR, year INTEGER,
    population DOUBLE, households DOUBLE,
    median_household_income DOUBLE,
    median_home_value DOUBLE, median_gross_rent DOUBLE,
    PRIMARY KEY (fips_county, year)
);
CREATE TABLE IF NOT EXISTS fhfa_hpi_metro (
    cbsa_code VARCHAR, period DATE, hpi DOUBLE,
    PRIMARY KEY (cbsa_code, period)
);
CREATE TABLE IF NOT EXISTS saiz_elasticity (
    cbsa_name VARCHAR PRIMARY KEY, elasticity DOUBLE,
    state VARCHAR
);
CREATE TABLE IF NOT EXISTS wharton_wrluri (
    cbsa_name VARCHAR PRIMARY KEY, wrluri_2018 DOUBLE,
    state VARCHAR
);
CREATE TABLE IF NOT EXISTS property_tax_state (
    state VARCHAR PRIMARY KEY,
    effective_rate_pct DOUBLE   -- Lincoln Institute / Tax Foundation effective rate
);
CREATE TABLE IF NOT EXISTS msa_archetype (
    cbsa_code VARCHAR PRIMARY KEY,
    archetype VARCHAR
);
CREATE TABLE IF NOT EXISTS property_alpha (
    mls VARCHAR PRIMARY KEY,
    flag_fixer_upper BOOLEAN, flag_distressed BOOLEAN,
    flag_long_dom BOOLEAN, flag_price_cuts BOOLEAN,
    flag_motivated_language BOOLEAN, flag_assumable BOOLEAN,
    flag_oz BOOLEAN, flag_adu_eligible BOOLEAN,
    flag_information_avm BOOLEAN,
    arv_estimate DOUBLE, rehab_estimate DOUBLE,
    max_70_rule_bid DOUBLE,
    alpha_stack INTEGER,
    pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS redfin_listings (
    mls VARCHAR PRIMARY KEY,
    region VARCHAR, url VARCHAR, street_address VARCHAR,
    city VARCHAR, state VARCHAR, zip VARCHAR,
    listed_price DOUBLE, beds DOUBLE, baths DOUBLE, sqft DOUBLE,
    lot_size DOUBLE, year_built INTEGER, hoa DOUBLE,
    days_on_market INTEGER, monthly_expense DOUBLE,
    pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def upsert_df(con: duckdb.DuckDBPyConnection, table: str, df) -> int:
    """Upsert a pandas DataFrame into table by replacing matching PK rows."""
    if df is None or df.empty:
        return 0
    con.register("_staging", df)
    cols = ", ".join(df.columns)
    con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _staging")
    con.unregister("_staging")
    return len(df)
