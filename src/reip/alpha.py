"""Property-level alpha overlay (§5 of the framework).

Computes the eight alpha flags per property for the redfin_listings table:

  1. Physical value-add  (fixer-upper, BRRRR)
  2. Distressed seller   (probate, NOD, tax delinquency, code)
  3. Operational lift    (multifamily — handled at deal-level, not flagged here)
  4. Use-change          (ADU eligibility, STR-eligible market)
  5. Capital structure   (assumable / subject-to)
  6. Tax-driven          (OZ-located, REPS-eligible)
  7. Information / data  (Zestimate vs comp delta, climate underprice)
  8. Behavioral          (long DOM + price cuts + motivated language)

Free-text MLS remarks aren't in our store yet; we flag what can be derived
from the listing fields we do have, plus an ARV / 70%-rule estimate that is
the canonical BRRRR underwriting heuristic.
"""
from __future__ import annotations
import duckdb
import pandas as pd
from .store import connect, upsert_df
from . import remarks as remarks_mod, avm as avm_mod


def _arv_estimate(price: float, sqft: float, market_psf: float) -> float:
    """ARV ≈ sqft × market $/sqft (90th-pct comp). Fallback to listed price."""
    if pd.notna(sqft) and pd.notna(market_psf) and market_psf > 0:
        return float(sqft) * float(market_psf)
    return float(price) if pd.notna(price) else float("nan")


def _rehab_estimate(price: float, sqft: float, year_built, dom: float) -> float:
    """Crude rehab estimate: $30/sqft baseline scaled by age + DOM stress flags.
    For a real platform this is replaced by a parts-and-labor model and
    photo-driven heuristics."""
    if pd.isna(sqft) or sqft <= 0:
        return float("nan")
    base = 30.0
    if pd.notna(year_built) and year_built < 1960:
        base += 20
    elif pd.notna(year_built) and year_built < 1985:
        base += 10
    if pd.notna(dom) and dom > 90:
        base += 5
    return float(sqft) * base


def compute(con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    own = False
    if con is None:
        con = connect(); own = True
    try:
        listings = con.execute("SELECT * FROM redfin_listings").df()
        if listings.empty:
            return listings
        # Market $/sqft from Redfin Data Center zip-level (last 90d)
        psf = con.execute(
            """SELECT geo_id AS zip, AVG(median_sale_price) / NULLIF(AVG(NULLIF(inventory,0)), 0) AS dummy
               FROM redfin_market WHERE geo_type='zip' GROUP BY geo_id"""
        ).df()  # placeholder — Redfin schema doesn't ship median_ppsf in our keep
        # Use ZHVI / typical sqft (1800) as a rough psf proxy for now
        zhvi = con.execute(
            """SELECT zip, value AS zhvi FROM zillow_zhvi WHERE (zip, period) IN
               (SELECT zip, MAX(period) FROM zillow_zhvi GROUP BY zip)"""
        ).df()
        zhvi["market_psf"] = zhvi["zhvi"] / 1800.0
        listings = listings.merge(zhvi[["zip", "market_psf"]], on="zip", how="left")
    finally:
        if own:
            con.close()

    listings["arv_estimate"] = listings.apply(
        lambda r: _arv_estimate(r["listed_price"], r["sqft"], r["market_psf"]), axis=1
    )
    listings["rehab_estimate"] = listings.apply(
        lambda r: _rehab_estimate(r["listed_price"], r["sqft"], r["year_built"], r["days_on_market"]), axis=1
    )
    listings["max_70_rule_bid"] = 0.70 * listings["arv_estimate"] - listings["rehab_estimate"]

    listings["flag_long_dom"] = listings["days_on_market"].fillna(0) > 60
    listings["flag_price_cuts"] = False  # needs price history (RESO `price_change_timestamp`)

    # Free-text remarks (Redfin scrape doesn't ship them yet, but if a future
    # listing carries `public_remarks` we run the regex parser to populate the
    # behavioral / use-change / assumable flags from language alone).
    if "public_remarks" in listings.columns:
        sigs = listings["public_remarks"].apply(remarks_mod.parse)
        listings["flag_motivated_language"] = sigs.apply(lambda s: s.motivated)
        listings["flag_assumable"] = sigs.apply(lambda s: s.assumable)
        # use-change from remarks promotes the existing state-level proxy
        remarks_use_change = sigs.apply(lambda s: s.use_change)
    else:
        listings["flag_motivated_language"] = False
        listings["flag_assumable"] = False
        remarks_use_change = False

    # Fixer-upper flag = listed below ARV − rehab buffer
    listings["flag_fixer_upper"] = (
        listings["listed_price"] < (listings["arv_estimate"] - listings["rehab_estimate"]) * 0.80
    ).fillna(False)
    # Distressed flag: long DOM + listed below market psf comp
    listings["flag_distressed"] = (
        listings["flag_long_dom"]
        & (listings["listed_price"] < listings["arv_estimate"] * 0.85)
    ).fillna(False)
    # OZ overlay still TODO (paid)
    listings["flag_oz"] = False
    listings["flag_adu_eligible"] = (
        listings["state"].isin(["CA", "OR", "WA", "MN", "CO"]) | remarks_use_change
    )

    # AVM information-alpha overlay: tag listings sitting in 'cold' zips
    # (Redfin sales clearing meaningfully below ZHVI) as buy candidates.
    try:
        avm = avm_mod.compute(con) if not own else None
        if avm is None:
            from .store import connect as _c
            with _c() as _con2:
                avm = avm_mod.compute(_con2)
        if not avm.empty:
            listings = listings.merge(
                avm[["zip", "divergence_z", "direction"]], on="zip", how="left"
            )
            listings["flag_information_avm"] = listings["direction"].eq("cold")
        else:
            listings["flag_information_avm"] = False
    except Exception:
        listings["flag_information_avm"] = False

    flag_cols = [
        "flag_fixer_upper", "flag_distressed", "flag_long_dom", "flag_price_cuts",
        "flag_motivated_language", "flag_assumable", "flag_oz", "flag_adu_eligible",
        "flag_information_avm",
    ]
    listings["alpha_stack"] = listings[flag_cols].astype(int).sum(axis=1)

    return listings[
        ["mls"] + flag_cols
        + ["arv_estimate", "rehab_estimate", "max_70_rule_bid", "alpha_stack"]
    ]


def persist(con: duckdb.DuckDBPyConnection) -> int:
    df = compute(con)
    if df.empty:
        return 0
    return upsert_df(con, "property_alpha", df)
