"""Property-ingest tests — offline only (no live HTTP).

We stub the network layer with monkeypatch. The point of these tests is
the *control flow* — making sure that the ingest never silently returns
a wrong listing, even when the underlying scrape fails.
"""
from __future__ import annotations
import pytest
import httpx
import duckdb
import pandas as pd
from reip.store import init, upsert_df
from reip import property_ingest as pi


def test_detect_source():
    assert pi.detect_source("https://www.redfin.com/CA/Berkeley/123/home/4") == "redfin"
    assert pi.detect_source("https://www.zillow.com/homedetails/123/4_zpid/") == "zillow"
    assert pi.detect_source("https://www.realtor.com/realestateandhomes-detail/123") == "realtor"
    assert pi.detect_source("https://example.com/foo") == "unknown"


def test_unsupported_source_returns_warning():
    p = pi.ingest("https://example.com/foo")
    assert p.source == "unknown"
    assert p.warnings
    assert all(v is None for v in [p.address, p.listed_price, p.sqft])


def test_empty_url_returns_warning():
    p = pi.ingest("")
    assert p.source == "unknown"
    assert "invalid URL" in (p.warnings or [""])[0]


class _FakeRequest:
    pass


class _FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text
        self.request = _FakeRequest()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=self.request, response=self)


def test_redfin_403_does_not_pull_wrong_listing(monkeypatch):
    """If stingray returns 403 (rate-limited / region-blocked), we MUST NOT
    silently fall back to HTML scraping — that returns a different
    property's data and the user underwrites the wrong house."""
    def fake_get(url, *a, **kw):
        # All Redfin endpoints return 403 in this test
        if "redfin.com" in url:
            return _FakeResponse(403)
        return _FakeResponse(200, text="<html></html>")
    monkeypatch.setattr(httpx, "get", fake_get)

    p = pi.ingest("https://www.redfin.com/CA/Berkeley/2616-Park-St-94702/home/1604873")
    # We learned the URL zip; rest is None.
    assert p.zip == "94702"
    assert p.address is None
    assert p.listed_price is None
    assert p.beds is None
    assert p.sqft is None
    # And we tell the user clearly what happened.
    assert any("403" in w or "blocked" in w.lower() for w in p.warnings)


def test_redfin_404_returns_clean_listing_not_found(monkeypatch):
    def fake_get(url, *a, **kw):
        return _FakeResponse(404)
    monkeypatch.setattr(httpx, "get", fake_get)

    p = pi.ingest("https://www.redfin.com/CA/Berkeley/2616-Park-St-94702/home/1604873")
    assert p.address is None
    assert any("404" in w or "not found" in w.lower() for w in p.warnings)


def test_zillow_blocked_returns_clean(monkeypatch):
    def fake_get(url, *a, **kw):
        return _FakeResponse(403)
    monkeypatch.setattr(httpx, "get", fake_get)

    p = pi.ingest("https://www.zillow.com/homedetails/123-Main-St/12345_zpid/")
    # Zillow blocks aggressively in many regions; we should fail open with a warning.
    assert p.source == "zillow"
    assert any("block" in w.lower() or "region" in w.lower() for w in p.warnings)
    assert p.listed_price is None


def test_zori_fallback_returns_estimate():
    con = duckdb.connect(":memory:")
    init(con)
    upsert_df(con, "zillow_zori", pd.DataFrame([
        {"zip": "94702", "period": pd.Timestamp("2026-04-30").date(), "value": 2750.0},
        {"zip": "94702", "period": pd.Timestamp("2026-03-31").date(), "value": 2700.0},
    ]))
    rent = pi.rent_estimate_from_zori(con, "94702")
    assert rent == 2750.0


def test_zori_fallback_returns_none_when_zip_missing():
    con = duckdb.connect(":memory:")
    init(con)
    assert pi.rent_estimate_from_zori(con, "99999") is None
    assert pi.rent_estimate_from_zori(con, "") is None
    assert pi.rent_estimate_from_zori(con, None) is None
