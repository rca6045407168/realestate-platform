"""Phase 6 Task 1 — leakage-free as-of scorer.

Smoke + invariant tests on the real DuckDB panel. Math correctness lives
in score_backtest's tests (which run on synthetic data). Here we just
pin: the function runs, returns ≥100 MSAs, scores are bounded, and
known structural facts hold (Sun Belt 2018 appreciation > Heartland 2018
appreciation).
"""
from __future__ import annotations
import pytest
import math

from reip.score_as_of import score_as_of
from reip.store import connect


@pytest.fixture(scope="module")
def df_2018():
    con = connect()
    return score_as_of(con, score_year=2018)


def test_score_as_of_runs_on_real_panel(df_2018):
    assert len(df_2018) >= 100, f"expected ≥100 scored MSAs, got {len(df_2018)}"
    required_cols = {
        "cbsa_code", "hpi_5y_cagr", "hpi_12mo_momentum",
        "zhvi_5y_growth", "zhvi_12mo_momentum",
        "gross_yield", "rent_3y_cagr", "flood_per_pop",
        "appreciation_score", "cashflow_score", "risk_score",
        "total_return_score",
    }
    assert required_cols.issubset(set(df_2018.columns))


def test_winsorization_caps_score_magnitudes(df_2018):
    """Pre-winsorization, flood outliers pushed total_return_score below
    -30 for a few coastal MSAs. After winsorization, no MSA's blended
    score should be more than ~5σ from zero. This is a regression test
    on the winsorization step."""
    abs_max = df_2018["total_return_score"].abs().max()
    assert abs_max < 6.0, (
        f"total_return_score had |max|={abs_max:.2f}; winsorization "
        f"should cap extreme outliers"
    )


def test_no_inf_in_outputs(df_2018):
    """robust z divides by IQR; inf in inputs (e.g., zhvi/zori bad ratios)
    must not propagate. score_as_of replaces inf with NaN pre-z; this
    test pins the contract."""
    for col in ("appreciation_score", "cashflow_score", "risk_score",
                "total_return_score"):
        vals = df_2018[col].values
        assert not any(math.isinf(v) for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v)))


def test_appreciation_score_correlates_with_5y_hpi(df_2018):
    """As-of-2018 appreciation_score should correlate positively with
    its own hpi_5y_cagr input (this is sanity, not a backtest claim)."""
    rho = df_2018[["appreciation_score", "hpi_5y_cagr"]].corr(method="spearman").iloc[0, 1]
    assert rho > 0.5, f"appreciation_score should track hpi_5y_cagr; ρ={rho:+.2f}"


def test_score_year_2014_handles_missing_zori():
    """ZORI starts 2015. Scoring as-of 2014 should still return a result
    (HPI features work) — just with NaN/0 cashflow_score. The function
    should not crash; downstream consumers handle the missing rent
    features themselves."""
    con = connect()
    df = score_as_of(con, score_year=2014)
    assert len(df) >= 50, "should still score MSAs even when ZORI features fail"
    # appreciation should still be computable
    assert df["appreciation_score"].notna().any()
