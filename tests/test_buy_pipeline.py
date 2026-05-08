"""Tests for the buy-ideas pipeline (listings_search, projection, decision).

No live HTTP. We stub Redfin search and seed an in-memory duckdb with
minimum ZHVI/ZORI rows so projection has data to chew on.
"""
from __future__ import annotations
import json
import pytest
import httpx
import duckdb
import pandas as pd
from reip.store import init, upsert_df
from reip import listings_search, projection, decision, recommendation as rec


FAKE_REDFIN_ENVELOPE = """{}&&""" + json.dumps({
    "version": 640, "errorMessage": "Success", "resultCode": 0,
    "payload": {
        "homes": [{
            "mlsId": {"value": "TEST-1", "label": "MLS#"},
            "url": "/TN/Memphis/123-Test-St-38127/home/123",
            "streetLine": {"value": "123 Test St"},
            "city": "Memphis", "state": "TN", "zip": "38127",
            "price": {"value": 95000},
            "beds": 3, "baths": 1,
            "sqFt": {"value": 1100},
            "yearBuilt": {"value": 1955},
            "lotSize": {"value": 5000},
            "hoa": {"value": 0},
            "timeOnRedfin": {"value": 12 * 24 * 60 * 60 * 1000},
        }]
    },
})


class _FakeResp:
    status_code = 200
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


def test_listings_search_parses_envelope(monkeypatch):
    def fake_get(url, *a, **kw):
        return _FakeResp(FAKE_REDFIN_ENVELOPE)
    monkeypatch.setattr(httpx, "get", fake_get)
    listings, warnings = listings_search.search("32820", num_homes=1)
    assert warnings == []
    assert len(listings) == 1
    L = listings[0]
    assert L.address == "123 Test St"
    assert L.zip == "38127"
    assert L.listed_price == 95000
    assert "Memphis" in L.cbsa_name
    assert L.days_on_market == 12


def test_listings_search_unknown_cbsa_returns_warning():
    listings, warnings = listings_search.search("99999")
    assert listings == []
    assert any("allowlist" in w for w in warnings)


def _seed(con, zhvi_base: float = 80_000.0, zori: float = 1_300.0,
          zip_code: str = "38127"):
    init(con)
    # Five years of monthly ZHVI rising 3%/yr
    rows = []
    base_date = pd.Timestamp("2026-04-30")
    for m in range(60):
        d = (base_date - pd.DateOffset(months=m)).date()
        rows.append({"zip": zip_code, "period": d,
                     "value": zhvi_base * (1.03 ** ((59 - m) / 12.0))})
    upsert_df(con, "zillow_zhvi", pd.DataFrame(rows))
    upsert_df(con, "zillow_zori", pd.DataFrame([
        {"zip": zip_code, "period": base_date.date(), "value": zori}
    ]))
    return con


def test_projection_returns_sane_5yr_numbers():
    """Memphis-archetype turnkey: $80k home, $1,300 rent (price/rent ~ 62)
    cash flows positive at 7% / 75% LTV."""
    con = _seed(duckdb.connect(":memory:"))
    listing = {
        "listed_price": 80_000, "sqft": 1100, "beds": 3, "baths": 1,
        "year_built": 1955, "hoa_monthly": 0, "zip": "38127",
    }
    p = projection.project(con, listing, archetype="Cashflow Heartland",
                            mortgage_rate=0.07, ltv=0.75)
    # 5-yr appreciation pulled from ZHVI → should be in the 1–7% CAGR band
    # after the Cashflow-Heartland 0.75 archetype haircut.
    assert 0.001 < p.appreciation_cagr < 0.05
    # 5y rental profit positive at $1300/mo on an $80k house with 75% LTV
    assert p.rental_profit_5y > 0, f"got {p.rental_profit_5y}"
    # Equity paydown is real (loan amortizing)
    assert p.equity_paydown_5y > 0
    # Total >> 0
    assert p.total_return_5y_dollars > 5_000
    # IRR clears 5%
    assert p.irr_5y > 0.05
    # DSCR + cap rate sanity
    assert p.dscr_y1 >= 1.0 and p.cap_rate_y1 > 0.05
    assert "ZORI:zip" in p.sources


def test_projection_negative_carry_when_rent_too_low():
    """Same house but rent only $900 → DSCR < 1, rental profit < 0."""
    con = _seed(duckdb.connect(":memory:"), zori=900.0)
    listing = {"listed_price": 95_000, "zip": "38127",
               "sqft": 1100, "beds": 3, "baths": 1,
               "year_built": 1990, "hoa_monthly": 0}
    p = projection.project(con, listing, archetype="Cashflow Heartland")
    assert p.dscr_y1 < 1.0
    assert p.rental_profit_5y < 0


def test_decision_text_includes_archetype_and_5y_dollars():
    p = projection.Projection(
        appreciation_cagr=0.03, appreciation_5y_pct=0.16, appreciation_5y_dollars=15_000,
        rental_profit_5y=20_000, equity_paydown_5y=5_000,
        total_return_5y_dollars=40_000, total_return_5y_pct=1.0,
        irr_5y=0.18, cash_on_cash_y1=0.10, dscr_y1=1.45, cap_rate_y1=0.085,
        vacancy_used=0.078, vacancy_source="acs:zip-county",
        sources=["ZORI:zip"],
    )
    listing = {"cbsa_name": "Memphis", "listed_price": 95_000}
    d = decision.build(
        listing=listing, projection=p,
        archetype="Cashflow Heartland",
        msa_appreciation_score=-0.10, msa_cashflow_score=0.10,
        avm_direction="aligned", avm_z=-0.5,
        rec_verdict="GREEN", rec_reasons=["All thresholds clear."],
        rec_primary_action="Submit offer at modeled price",
    )
    blob = " ".join(d.reasons)
    assert "Memphis" in blob and "Cashflow Heartland" in blob
    # Mentions the 5y appreciation dollars + rental profit
    assert "$15k" in blob or "$15,000" in blob.replace(",", "")
    assert "$20k" in blob or "~$20" in blob
    assert d.thesis_tag == "yield-driven"
    assert d.verdict == "GREEN"


def test_decision_red_verdict_says_pass():
    p = projection.Projection(
        appreciation_cagr=0.03, appreciation_5y_pct=0.16, appreciation_5y_dollars=15_000,
        rental_profit_5y=-500, equity_paydown_5y=5_000,
        total_return_5y_dollars=19_500, total_return_5y_pct=0.5,
        irr_5y=0.04, cash_on_cash_y1=-0.02, dscr_y1=0.94, cap_rate_y1=0.05,
        vacancy_used=0.05, vacancy_source="default-5pct",
        sources=["ZORI:zip"],
    )
    d = decision.build(
        listing={"cbsa_name": "Memphis", "listed_price": 160_000},
        projection=p, archetype="Cashflow Heartland",
        msa_appreciation_score=-0.10, msa_cashflow_score=0.10,
        avm_direction=None, avm_z=None,
        rec_verdict="RED", rec_reasons=["Stabilized DSCR is 0.94×, below the 1.10× floor for YELLOW."],
        rec_primary_action="Pass on this deal.",
    )
    assert d.verdict == "RED"
    # Cash-flow line should explicitly call out negative carry
    assert any("negative" in r.lower() or "won't cover" in r.lower() for r in d.reasons)
    assert "pass" in d.primary_action.lower()
