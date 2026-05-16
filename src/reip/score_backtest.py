"""Phase 6 — score-model backtest harness.

Implements the §7.5 protocol from `RealEstate_Investment_Framework_v5`:

  Test 1 — Quintile spread (top-vs-bottom ≥200 bps annualized at 95% CI via bootstrap)
  Test 2 — Decomposition validity (appreciation_score predicts FHFA;
           cashflow_score predicts yield)
  Test 3 — Lift over a single-factor benchmark (≥50 bps over naive yield)

Honest scope: reip's `msa_score.score()` is computed on CURRENT data, not
on as-of-date snapshots. So the backtest is **in-sample** unless we
build historical input snapshots (build-spec §3 Phase 6 task 1). This
module's `is_in_sample=True` flag carries that caveat into the report.

Distinct from `backtest.py` (golden-ranking sanity test on archetypes /
score direction) and `strategy.py::strategy_backtest` (50-year archetype
portfolio backtests). This module is the SCORE-MODEL backtest — does the
ranking from `msa_score.score()` actually pick winners?

Per spec discipline: results are published regardless of outcome. Reports
land at `~/.reip/backtest_reports/<date>.md`.
"""
from __future__ import annotations
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import msa_score
from .store import connect


# ---------------------------------------------------------------------------
# Realized returns — FHFA HPI for appreciation, ZORI/ZHVI yield for cashflow
# ---------------------------------------------------------------------------

def realized_hpi_cagr(con, start_year: int, window_years: int = 5) -> pd.DataFrame:
    """For each CBSA in fhfa_hpi_metro, compute HPI CAGR over the window.

    Returns DataFrame: cbsa_code, hpi_start, hpi_end, hpi_cagr.

    HPI is a Laspeyres index; CAGR ≈ realized price appreciation before
    leverage or rent income.
    """
    end_year = start_year + window_years
    sql = """
    WITH starts AS (
        SELECT cbsa_code, AVG(hpi) AS hpi_start
        FROM fhfa_hpi_metro
        WHERE year(period) = ?
        GROUP BY cbsa_code
    ),
    ends AS (
        SELECT cbsa_code, AVG(hpi) AS hpi_end
        FROM fhfa_hpi_metro
        WHERE year(period) = ?
        GROUP BY cbsa_code
    )
    SELECT s.cbsa_code,
           s.hpi_start,
           e.hpi_end,
           pow(e.hpi_end / NULLIF(s.hpi_start, 0), 1.0 / ?) - 1 AS hpi_cagr
    FROM starts s JOIN ends e USING (cbsa_code)
    WHERE s.hpi_start > 0
    """
    return con.execute(sql, [start_year, end_year, float(window_years)]).df()


def realized_msa_yield_window(
    con,
    start_year: int,
    window_years: int = 5,
) -> pd.DataFrame:
    """Per-CBSA gross yield averaged OVER the backtest window.

    Critical methodological point: using today's yield instead of the
    window's biases the test against appreciation-leading markets (which
    saw their yields compress as prices ran). We compute the average
    monthly (ZORI × 12) / ZHVI over each month in the window, then take
    a median across zips for the MSA aggregate.
    """
    end_year = start_year + window_years
    sql = """
    WITH window_zori AS (
        SELECT zip, period, value AS zori
        FROM zillow_zori
        WHERE year(period) BETWEEN ? AND ?
    ),
    window_zhvi AS (
        SELECT zip, period, value AS zhvi
        FROM zillow_zhvi
        WHERE year(period) BETWEEN ? AND ?
    ),
    monthly AS (
        SELECT z.zip, z.period, z.zori, h.zhvi,
               (z.zori * 12.0) / NULLIF(h.zhvi, 0) AS monthly_yield
        FROM window_zori z JOIN window_zhvi h USING (zip, period)
    ),
    avg_by_zip AS (
        SELECT zip,
               AVG(zori) AS zori_avg,
               AVG(zhvi) AS zhvi_avg,
               AVG(monthly_yield) AS yield_avg
        FROM monthly
        GROUP BY zip
    ),
    by_cbsa AS (
        SELECT cbsa.cbsa_code,
               median(b.zori_avg) AS zori_med,
               median(b.zhvi_avg) AS zhvi_med,
               median(b.yield_avg) AS gross_yield
        FROM avg_by_zip b
        JOIN zip_county_xwalk zc USING (zip)
        JOIN county_cbsa_xwalk cbsa ON cbsa.fips_county = zc.fips_county
        WHERE cbsa.cbsa_code IS NOT NULL
        GROUP BY cbsa.cbsa_code
    )
    SELECT cbsa_code, zori_med, zhvi_med, gross_yield
    FROM by_cbsa
    """
    return con.execute(sql, [start_year, end_year, start_year, end_year]).df()


def realized_total_return(con, start_year: int, window_years: int = 5) -> pd.DataFrame:
    """HPI CAGR + window-averaged gross yield as a total-return proxy.

    Convention: total = hpi_cagr + gross_yield_during_window. Un-levered,
    ignoring tax shield + principal paydown + cap-rate movement. The
    build spec asks for cap-rate movement too; that's a refinement of
    this baseline.
    """
    h = realized_hpi_cagr(con, start_year, window_years)
    y = realized_msa_yield_window(con, start_year, window_years)
    out = h.merge(y, on="cbsa_code", how="inner")
    out["total_return"] = out["hpi_cagr"] + out["gross_yield"]
    return out


# ---------------------------------------------------------------------------
# Test 1 — Quintile spread with bootstrap 95% CI on top-minus-bottom
# ---------------------------------------------------------------------------

@dataclass
class QuintileResult:
    score_col: str
    return_col: str
    n_msas: int
    quintile_means: list[float]
    quintile_counts: list[int]
    top_minus_bottom: float
    spread_ci_95: tuple[float, float]
    p_value_one_sided: float
    passes_200bps_test: bool


def _bucket_descending(scores: np.ndarray, n_buckets: int = 5) -> np.ndarray:
    """Rank descending; bucket 0 = top, bucket n_buckets-1 = bottom."""
    n = len(scores)
    order = np.argsort(-scores)
    bucket = np.empty(n, dtype=int)
    for k in range(n):
        bucket[order[k]] = min(n_buckets - 1, k * n_buckets // n)
    return bucket


def quintile_spread(
    scored: pd.DataFrame,
    returns: pd.DataFrame,
    score_col: str = "total_return_score",
    return_col: str = "total_return",
    n_bootstrap: int = 1_000,
    seed: int = 42,
) -> QuintileResult:
    """Rank MSAs by `score_col`, bucket into 5 quintiles, return mean
    `return_col` per bucket plus bootstrap 95% CI on top-minus-bottom.

    Acceptance: 200-bps annualized spread at the 95% CI lower bound,
    per build spec §7.5 Test 1.
    """
    df = scored[["cbsa_code", score_col]].merge(
        returns[["cbsa_code", return_col]],
        on="cbsa_code", how="inner",
    ).dropna()
    if len(df) < 25:
        return QuintileResult(
            score_col=score_col, return_col=return_col,
            n_msas=len(df), quintile_means=[], quintile_counts=[],
            top_minus_bottom=float("nan"),
            spread_ci_95=(float("nan"), float("nan")),
            p_value_one_sided=float("nan"),
            passes_200bps_test=False,
        )
    arr_score = df[score_col].to_numpy()
    arr_ret   = df[return_col].to_numpy()
    bucket = _bucket_descending(arr_score)
    means = [float(arr_ret[bucket == i].mean()) for i in range(5)]
    counts = [int((bucket == i).sum()) for i in range(5)]
    spread = means[0] - means[-1]

    # Bootstrap
    rng = np.random.default_rng(seed)
    n = len(df)
    spreads = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        s = arr_score[idx]
        r = arr_ret[idx]
        b = _bucket_descending(s)
        top_mean = r[b == 0].mean()
        bot_mean = r[b == 4].mean()
        spreads[i] = top_mean - bot_mean
    ci_lo, ci_hi = np.percentile(spreads, [2.5, 97.5])
    p_one_sided = float((spreads <= 0).mean())
    passes = bool(ci_lo >= 0.02)

    return QuintileResult(
        score_col=score_col,
        return_col=return_col,
        n_msas=int(len(df)),
        quintile_means=means,
        quintile_counts=counts,
        top_minus_bottom=float(spread),
        spread_ci_95=(float(ci_lo), float(ci_hi)),
        p_value_one_sided=p_one_sided,
        passes_200bps_test=passes,
    )


# ---------------------------------------------------------------------------
# Test 2 — Decomposition validity
# ---------------------------------------------------------------------------

def decomposition_validity(scored: pd.DataFrame, returns: pd.DataFrame) -> dict:
    """Spearman rank correlation: appreciation_score ↔ hpi_cagr (positive)
    and cashflow_score ↔ gross_yield (positive).
    """
    df = scored.merge(returns, on="cbsa_code", how="inner").dropna(
        subset=["appreciation_score", "cashflow_score", "hpi_cagr"]
    )
    yield_col = "gross_yield_y" if "gross_yield_y" in df.columns else "gross_yield"
    appr_rho = df[["appreciation_score", "hpi_cagr"]].corr(method="spearman").iloc[0, 1]
    cash_rho = df[["cashflow_score", yield_col]].corr(method="spearman").iloc[0, 1]
    return {
        "n_msas": int(len(df)),
        "appreciation_vs_hpi_spearman": float(appr_rho),
        "cashflow_vs_yield_spearman":   float(cash_rho),
        "passes": bool(appr_rho > 0 and cash_rho > 0),
    }


# ---------------------------------------------------------------------------
# Test 3 — Lift over single-factor benchmark
# ---------------------------------------------------------------------------

def single_factor_benchmark_lift(
    scored: pd.DataFrame,
    returns: pd.DataFrame,
    return_col: str = "total_return",
    n_bootstrap: int = 1_000,
) -> dict:
    """Compare the blended-score quintile spread to a naive-yield
    benchmark spread. Build spec §7.5 Test 3 asks for ≥50 bps lift.
    """
    model = quintile_spread(scored, returns,
                            score_col="total_return_score",
                            return_col=return_col,
                            n_bootstrap=n_bootstrap)
    # Benchmark: use realized gross_yield as the score directly.
    bench_scored = returns[["cbsa_code", "gross_yield"]].rename(
        columns={"gross_yield": "naive_yield_score"}
    )
    bench = quintile_spread(bench_scored, returns,
                            score_col="naive_yield_score",
                            return_col=return_col,
                            n_bootstrap=n_bootstrap)
    lift = model.top_minus_bottom - bench.top_minus_bottom
    return {
        "model_spread": model.top_minus_bottom,
        "benchmark_spread": bench.top_minus_bottom,
        "lift": lift,
        "lift_passes_50bps": bool(lift >= 0.005),
        "benchmark_col": "gross_yield",
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

@dataclass
class BacktestReport:
    score_date: str
    backtest_start_year: int
    window_years: int
    is_in_sample: bool
    n_msas: int
    test_1_quintile_spread: dict
    test_2_decomposition: dict
    test_3_lift_over_yield: dict
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**self.__dict__}


def run_backtest(
    con=None,
    backtest_start_year: int = 2018,
    window_years: int = 5,
    n_bootstrap: int = 1_000,
) -> BacktestReport:
    """End-to-end backtest run. Default window: 2018→2023 (most recent
    complete 5y the FHFA panel cleanly supports given current data lag).
    """
    own = False
    if con is None:
        con = connect(); own = True
    try:
        raw = msa_score.features(con)
        scored = msa_score.with_archetype(msa_score.score(raw))
        returns = realized_total_return(con, backtest_start_year, window_years)

        q = quintile_spread(scored, returns,
                            score_col="total_return_score",
                            return_col="total_return",
                            n_bootstrap=n_bootstrap)
        d = decomposition_validity(scored, returns)
        lift = single_factor_benchmark_lift(scored, returns,
                                            return_col="total_return",
                                            n_bootstrap=n_bootstrap)

        notes = [
            "IN-SAMPLE: msa_score uses current-snapshot data, not as-of-"
            f"{backtest_start_year} inputs. True out-of-sample requires "
            "build-spec §3 Phase 6 task 1 (historical-factor snapshot).",
            f"Realized total return = FHFA HPI CAGR ({backtest_start_year}→"
            f"{backtest_start_year + window_years}) + current gross yield. "
            "Cap-rate movement + tax shield refinements deferred.",
        ]
        return BacktestReport(
            score_date=_dt.datetime.utcnow().strftime("%Y-%m-%d"),
            backtest_start_year=backtest_start_year,
            window_years=window_years,
            is_in_sample=True,
            n_msas=q.n_msas,
            test_1_quintile_spread=q.__dict__,
            test_2_decomposition=d,
            test_3_lift_over_yield=lift,
            notes=notes,
        )
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_report(report: BacktestReport, dest_dir: Optional[Path] = None) -> Path:
    """Write the markdown report under `~/.reip/backtest_reports/`.
    Filename includes window so multi-window runs don't clobber.
    Per build-spec §3 Phase 6: published regardless of outcome."""
    dest_dir = dest_dir or (Path.home() / ".reip" / "backtest_reports")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / (
        f"backtest-{report.score_date}-"
        f"{report.backtest_start_year}w{report.window_years}.md"
    )
    q  = report.test_1_quintile_spread
    d  = report.test_2_decomposition
    l3 = report.test_3_lift_over_yield
    md = []
    md.append(f"# reip score-model backtest — {report.score_date}")
    md.append("")
    md.append(
        f"Window: **{report.backtest_start_year}→{report.backtest_start_year + report.window_years}** "
        f"({report.window_years}y) | MSAs scored: **{report.n_msas}** | "
        f"In-sample: **{report.is_in_sample}**"
    )
    md.append("")
    md.append("## Test 1 — Quintile spread (top vs bottom)")
    md.append("")
    md.append(f"- Score column: `{q['score_col']}`")
    md.append(f"- Return column: `{q['return_col']}`")
    md.append(f"- Top-minus-bottom annualized: **{q['top_minus_bottom']*100:+.2f} pp**")
    ci_lo, ci_hi = q["spread_ci_95"]
    md.append(f"- Bootstrap 95% CI: [{ci_lo*100:+.2f} pp, {ci_hi*100:+.2f} pp]")
    md.append(f"- One-sided p-value (H0: spread ≤ 0): **{q['p_value_one_sided']:.4f}**")
    md.append(f"- Passes 200-bps acceptance: **{'YES' if q['passes_200bps_test'] else 'NO'}**")
    md.append("")
    md.append("Quintile means (top → bottom):")
    md.append("")
    md.append("| Quintile | N | Mean total return |")
    md.append("|---|---|---|")
    labels = ["Q1 (top)", "Q2", "Q3", "Q4", "Q5 (bottom)"]
    for lbl, m, n in zip(labels, q["quintile_means"], q["quintile_counts"]):
        md.append(f"| {lbl} | {n} | {m*100:+.2f}% |")
    md.append("")
    md.append("## Test 2 — Decomposition validity")
    md.append("")
    md.append(f"- Appreciation score ↔ HPI CAGR Spearman ρ: **{d['appreciation_vs_hpi_spearman']:+.3f}**")
    md.append(f"- Cashflow score ↔ gross yield Spearman ρ: **{d['cashflow_vs_yield_spearman']:+.3f}**")
    md.append(f"- Passes (both positive): **{'YES' if d['passes'] else 'NO'}**")
    md.append("")
    md.append("## Test 3 — Lift over single-factor benchmark")
    md.append("")
    md.append(f"- Benchmark: `{l3['benchmark_col']}` quintile spread")
    md.append(f"- Model spread: **{l3['model_spread']*100:+.2f} pp**")
    md.append(f"- Benchmark spread: **{l3['benchmark_spread']*100:+.2f} pp**")
    md.append(f"- Lift: **{l3['lift']*100:+.2f} pp**")
    md.append(f"- Passes 50-bps lift: **{'YES' if l3['lift_passes_50bps'] else 'NO'}**")
    md.append("")
    md.append("## Notes")
    md.append("")
    for n in report.notes:
        md.append(f"- {n}")
    md.append("")
    path.write_text("\n".join(md))
    return path


# ---------------------------------------------------------------------------
# Test 4 — Out-of-sample regime stability (multi-window orchestrator)
# ---------------------------------------------------------------------------

def run_backtest_multi_window(
    con=None,
    windows: list[tuple[int, int]] = [(2014, 5), (2018, 5)],
    n_bootstrap: int = 1_000,
    write: bool = True,
) -> dict:
    """Run the score-model backtest across multiple non-overlapping windows.
    Per build spec §7.5 Test 4: out-of-sample regime stability.

    Returns a dict with one BacktestReport per window plus a stability
    summary: signs-agree fraction across windows, mean spread.
    """
    own = False
    if con is None:
        con = connect(); own = True
    reports: list[BacktestReport] = []
    paths: list[Path] = []
    try:
        for (start, years) in windows:
            r = run_backtest(con,
                             backtest_start_year=start,
                             window_years=years,
                             n_bootstrap=n_bootstrap)
            reports.append(r)
            if write:
                paths.append(write_report(r))

        spreads = [r.test_1_quintile_spread["top_minus_bottom"] for r in reports]
        signs_positive = sum(1 for s in spreads if s > 0)
        return {
            "windows": [(r.backtest_start_year, r.window_years) for r in reports],
            "spreads": spreads,
            "mean_spread": float(np.mean(spreads)),
            "signs_positive": signs_positive,
            "all_positive": signs_positive == len(spreads),
            "test_2_appr_rho": [r.test_2_decomposition["appreciation_vs_hpi_spearman"] for r in reports],
            "test_2_cash_rho": [r.test_2_decomposition["cashflow_vs_yield_spearman"]   for r in reports],
            "test_3_lift":     [r.test_3_lift_over_yield["lift"]                       for r in reports],
            "report_paths":    [str(p) for p in paths],
        }
    finally:
        if own:
            con.close()
