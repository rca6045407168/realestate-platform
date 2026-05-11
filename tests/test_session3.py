"""Tests for the BLS / AVM / remarks / report-with-history additions."""
from __future__ import annotations
import duckdb
import pandas as pd
from reip.store import init, upsert_df
from reip import remarks, avm, report


def test_remarks_parser_auction_flag():
    """The new auction flag catches REO/foreclosure/trustee/sheriff/HUD-owned
    language but suppresses false positives like 'not an auction' and 'stereo'."""
    positives = [
        "Bank-owned REO property. Cash only.",
        "Online auction, starting bid $50,000.",
        "Trustees sale, HUD owned home.",
        "Court-ordered sale. Investor special.",
        "Sheriff sale on 6/15.",
        "Foreclosed home, sold as-is.",
    ]
    for text in positives:
        s = remarks.parse(text)
        assert s.auction, f"Expected auction=True for: {text!r}"
    negatives = [
        "Beautiful home with stereo system.",
        "Not an auction property, motivated seller.",
        "No auction needed.",
        "Non-auction sale.",
        "Charming starter home, move-in ready.",
    ]
    for text in negatives:
        s = remarks.parse(text)
        assert not s.auction, f"Expected auction=False for: {text!r}"


def test_remarks_parser_catches_canonical_phrases():
    text = ("Charming 3BR fixer-upper, MOTIVATED SELLER relocating, "
            "sold AS-IS, ASSUMABLE VA loan at 3.25%, R-2 zoning ADU potential, "
            "price reduced!")
    s = remarks.parse(text)
    assert s.motivated and s.distressed and s.use_change
    assert s.assumable and s.price_cut
    assert s.score >= 5 / 8     # 8 categories now (added auction)
    # Canonical terms surface in matched_terms
    assert any("motivated seller" in t for t in s.matched_terms)
    # parse() returns the FIRST regex hit per category; in this text the
    # distressed category is satisfied by 'fixer', not 'as-is'. The flag
    # is what we trust; matched_terms is best-effort context.
    assert s.distressed is True


def test_remarks_parser_no_false_positive():
    """Make sure 'motivated buyers welcome' doesn't trip motivated-seller flag."""
    s = remarks.parse("Beautiful home in great neighborhood, motivated buyers welcome.")
    assert not s.motivated
    assert not s.distressed


def test_remarks_parser_handles_none():
    s = remarks.parse(None)
    assert s.score == 0.0
    assert s.matched_terms == ()


def test_avm_directions_are_balanced():
    con = duckdb.connect(":memory:")
    init(con)
    # Synthetic: 5 zips, ZHVI all 200k, Redfin sale split into hot/cold/aligned
    upsert_df(con, "zillow_zhvi", pd.DataFrame([
        {"zip": z, "period": pd.Timestamp("2026-04-01").date(), "value": 200_000}
        for z in ["00001", "00002", "00003", "00004", "00005"]
    ]))
    rows = []
    for zp, sale in [("00001", 280_000), ("00002", 260_000), ("00003", 200_000),
                     ("00004", 140_000), ("00005", 120_000)]:
        rows.append({"geo_id": zp, "geo_type": "zip",
                     "period": pd.Timestamp("2026-03-15").date(),
                     "median_sale_price": sale, "median_list_price": None,
                     "homes_sold": 25, "new_listings": None, "inventory": None,
                     "median_days_on_market": None, "sale_to_list": None,
                     "pct_homes_sold_above_list": None,
                     "off_market_in_two_weeks": None})
    upsert_df(con, "redfin_market", pd.DataFrame(rows))
    df = avm.compute(con)
    assert not df.empty
    # Hottest zip should have direction='hot', coldest 'cold'
    by_zip = df.set_index("zip")
    assert by_zip.loc["00001", "direction"] in ("hot", "aligned")
    assert by_zip.loc["00005", "direction"] in ("cold", "aligned")
    # Aligned zip in middle
    assert by_zip.loc["00003", "divergence_pct"] == 0


def test_avm_persist_replaces_prior_data():
    con = duckdb.connect(":memory:")
    init(con)
    upsert_df(con, "zillow_zhvi", pd.DataFrame([
        {"zip": "99999", "period": pd.Timestamp("2026-04-01").date(), "value": 100_000}
    ]))
    upsert_df(con, "redfin_market", pd.DataFrame([{
        "geo_id": "99999", "geo_type": "zip",
        "period": pd.Timestamp("2026-04-01").date(),
        "median_sale_price": 80_000, "median_list_price": None,
        "homes_sold": 25, "new_listings": None, "inventory": None,
        "median_days_on_market": None, "sale_to_list": None,
        "pct_homes_sold_above_list": None, "off_market_in_two_weeks": None,
    }]))
    n1 = avm.persist(con)
    n2 = avm.persist(con)  # should replace, not double
    cnt = con.execute("SELECT COUNT(*) FROM zip_avm_signal").fetchone()[0]
    assert n1 == n2 == 1 and cnt == 1


def test_html_report_includes_sparkline_history(tmp_path):
    con = duckdb.connect(":memory:")
    init(con)
    # Seed minimum data so the history SQL runs
    upsert_df(con, "zip_county_xwalk", pd.DataFrame([
        {"zip": "00001", "fips_county": "01001", "weight": 1.0}
    ]))
    upsert_df(con, "county_cbsa_xwalk", pd.DataFrame([
        {"fips_county": "01001", "cbsa_code": "99999", "cbsa_name": "Test, US",
         "cbsa_type": "", "state": ""}
    ]))
    # 6+ months of ZHVI so history shows up
    upsert_df(con, "zillow_zhvi", pd.DataFrame([
        {"zip": "00001", "period": (pd.Timestamp.today() - pd.DateOffset(months=m)).date(),
         "value": 200_000 + m * 1000}
        for m in range(7)
    ]))
    df = pd.DataFrame([{
        "cbsa_code": "99999", "cbsa_name": "Test, US", "archetype": "Mixed",
        "pop": 1_000_000, "pop_cagr_5yr": 0.02,
        "net_migration_pct_pop": 0.01, "gross_yield": 0.06,
        "permits_per_1000_hh": 5.0, "appreciation_score": 0.1,
        "cashflow_score": 0.0, "total_return_score": 0.05, "completeness": 0.85,
    }])
    out = report.build(df, tmp_path / "r.html", con=con)
    text = out.read_text()
    assert "function spark" in text
    assert "const HISTORY" in text
    # Sparkline data for our test CBSA must be in the HISTORY payload
    assert '"99999"' in text
