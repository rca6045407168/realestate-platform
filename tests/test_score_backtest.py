"""Phase 6 score-model backtest — protocol tests.

The backtest's mechanics (quintile spread, bootstrap CI, decomposition
correlation, lift calculation) are deterministic given fixed inputs.
This file pins those mechanics on synthetic data so refactors can't
silently break the math. Real-data smoke tests live in
test_smoke.py / manual runs.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

from reip import score_backtest as sbk


def _synthetic_panel(n: int = 100, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build a synthetic scored / returns pair where the score's signal
    is monotone: cbsa_code N has total_return_score N (top scores =
    high N), and total_return = N + small noise. Top quintile beats
    bottom by a known margin — quintile_spread should detect it cleanly.
    """
    rng = np.random.default_rng(seed)
    codes = [f"{i:05d}" for i in range(n)]
    scored = pd.DataFrame({
        "cbsa_code": codes,
        "total_return_score": np.linspace(-1.0, 1.0, n),  # monotone
        "appreciation_score": np.linspace(-1.0, 1.0, n),
        "cashflow_score":     np.linspace(-1.0, 1.0, n),
    })
    returns = pd.DataFrame({
        "cbsa_code": codes,
        "hpi_cagr":  np.linspace(0.0, 0.10, n) + rng.normal(0, 0.005, n),
        "gross_yield": np.linspace(0.05, 0.10, n) + rng.normal(0, 0.002, n),
    })
    returns["total_return"] = returns["hpi_cagr"] + returns["gross_yield"]
    return scored, returns


def test_quintile_spread_detects_monotone_signal():
    """With perfectly monotone synthetic signal, the spread must be
    positive and the CI strictly above zero."""
    scored, returns = _synthetic_panel(n=100, seed=0)
    q = sbk.quintile_spread(scored, returns,
                            score_col="total_return_score",
                            return_col="total_return",
                            n_bootstrap=500)
    assert q.top_minus_bottom > 0.05, (
        f"Monotone signal must produce a clear spread; got "
        f"{q.top_minus_bottom:+.4f}"
    )
    ci_lo, ci_hi = q.spread_ci_95
    assert ci_lo > 0, f"CI lower bound must be > 0 for a clean signal; got {ci_lo}"
    assert q.p_value_one_sided < 0.05
    assert len(q.quintile_means) == 5
    # Top quintile mean > bottom quintile mean
    assert q.quintile_means[0] > q.quintile_means[-1]


def test_quintile_spread_handles_no_signal():
    """Pure-noise scores should NOT pass the 200bps test."""
    rng = np.random.default_rng(123)
    n = 100
    scored = pd.DataFrame({
        "cbsa_code": [f"{i:05d}" for i in range(n)],
        "total_return_score": rng.normal(size=n),
    })
    returns = pd.DataFrame({
        "cbsa_code": [f"{i:05d}" for i in range(n)],
        "total_return": rng.normal(loc=0.05, scale=0.02, size=n),
    })
    q = sbk.quintile_spread(scored, returns, n_bootstrap=500)
    # CI should straddle zero on pure noise → fails 200bps acceptance
    ci_lo, ci_hi = q.spread_ci_95
    assert ci_lo < 0.02, (
        f"Pure noise must fail the 200bps acceptance; got CI lower {ci_lo}"
    )


def test_quintile_spread_too_few_msas_returns_nan():
    """<25 MSAs → can't form 5 buckets of ≥5; return NaN result."""
    scored = pd.DataFrame({
        "cbsa_code": ["a", "b", "c"],
        "total_return_score": [0.1, 0.2, 0.3],
    })
    returns = pd.DataFrame({
        "cbsa_code": ["a", "b", "c"],
        "total_return": [0.05, 0.06, 0.07],
    })
    q = sbk.quintile_spread(scored, returns)
    import math
    assert math.isnan(q.top_minus_bottom)
    assert q.passes_200bps_test is False


def test_decomposition_validity_directionally_correct():
    """Spearman correlations should be positive on monotone synthetic data."""
    scored, returns = _synthetic_panel(n=100, seed=0)
    out = sbk.decomposition_validity(scored, returns)
    assert out["appreciation_vs_hpi_spearman"] > 0.5
    assert out["cashflow_vs_yield_spearman"] > 0.5
    assert out["passes"] is True


def test_single_factor_benchmark_lift_runs():
    """End-to-end mechanics of the lift calculation. Numerical magnitude
    is data-dependent; here we just pin the shape + sign of the lift
    field for the monotone case (model perfect → lift ≈ 0 since the
    model's score IS roughly the yield + appreciation)."""
    scored, returns = _synthetic_panel(n=100, seed=0)
    out = sbk.single_factor_benchmark_lift(scored, returns, n_bootstrap=300)
    assert "model_spread" in out
    assert "benchmark_spread" in out
    assert "lift" in out
    assert isinstance(out["lift_passes_50bps"], bool)


def test_report_writes_to_tmp(tmp_path):
    """Report writer must produce a non-empty markdown file at the
    expected path scheme."""
    scored, returns = _synthetic_panel(n=100, seed=0)
    # Hand-build a small report so we don't need DB access
    q = sbk.quintile_spread(scored, returns, n_bootstrap=300)
    d = sbk.decomposition_validity(scored, returns)
    l = sbk.single_factor_benchmark_lift(scored, returns, n_bootstrap=300)
    report = sbk.BacktestReport(
        score_date="2026-05-16",
        backtest_start_year=2018,
        window_years=5,
        is_in_sample=True,
        n_msas=q.n_msas,
        test_1_quintile_spread=q.__dict__,
        test_2_decomposition=d,
        test_3_lift_over_yield=l,
        notes=["synthetic test"],
    )
    path = sbk.write_report(report, dest_dir=tmp_path)
    assert path.exists()
    body = path.read_text()
    assert "# reip score-model backtest" in body
    assert "Test 1" in body and "Test 2" in body and "Test 3" in body
    assert "Quintile means" in body
    assert "Spearman" in body
    # Filename scheme: backtest-<date>-<start>w<years>-<IS|OOS>.md
    assert "backtest-2026-05-16-2018w5-IS.md" in str(path)
