"""Data-derived strategy backtests over 50 years of US housing.

Re-runs the analyses in docs/STRATEGY.md against current data, so the
doc's claims stay empirical. All numbers are computed live from
fhfa_hpi_metro / zillow_zhvi / zillow_zori.

Exposed entrypoints:
  - regime_decomposition()  — 8 regimes 1985-2024, dispersion stats
  - drawdown_panel()        — per-metro max DD + time-to-recover
  - momentum_persistence()  — 3y→3y quartile transition matrix
  - strategy_backtest()     — equal-weight strategy portfolios
  - rent_yield_panel()      — gross yield + price appreciation 2015-2024
  - full_report()           — runs all five and returns a single dict

Each function returns plain Python (lists/dicts/scalars) so it's JSON-
serializable and can be shipped through /api/strategy/backtest.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
import math


# ---- Default reference compositions (see docs/STRATEGY.md) -----------------

ALL_WEATHER     = ["13380", "11700", "31540", "20740", "34900",
                    "45460", "49340", "38860"]
CA_COASTAL      = ["41940", "41860", "31080", "41740", "40140"]
SUN_BELT_GROWTH = ["12060", "12420", "38060", "19100", "26420",
                    "33100", "36740", "34980"]
HEARTLAND_YIELD = ["17140", "17410", "18140", "26900", "32820",
                    "38300", "28140"]

STRATEGY_COMPOSITIONS = {
    "All-Weather":      ALL_WEATHER,
    "CA Coastal":       CA_COASTAL,
    "Sun Belt Growth":  SUN_BELT_GROWTH,
    "Heartland Yield":  HEARTLAND_YIELD,
}

# 8 regimes used in the synthesis doc. Years are inclusive endpoints.
REGIMES = [
    ("Late-80s coastal boom", 1985, 1990),
    ("S&L bust / recovery",   1990, 1996),
    ("Dot-com housing rise",  1996, 2001),
    ("2000s bubble inflate",  2001, 2006),
    ("GFC crash",             2006, 2012),
    ("Recovery + low rates",  2012, 2019),
    ("COVID boom",            2019, 2022),
    ("Rate shock",            2022, 2024),
]


# ---- internals -------------------------------------------------------------

def _annual_hpi_panel(con) -> pd.DataFrame:
    """Year-end FHFA HPI per CBSA, joined to name."""
    df = con.execute("""
        WITH names AS (
            SELECT DISTINCT cbsa_code, ANY_VALUE(cbsa_name) AS cbsa_name
            FROM county_cbsa_xwalk GROUP BY cbsa_code
        )
        SELECT h.cbsa_code,
               COALESCE(n.cbsa_name, '?') AS cbsa_name,
               EXTRACT(YEAR FROM h.period) AS year,
               LAST(h.hpi ORDER BY h.period) AS hpi
        FROM fhfa_hpi_metro h
        LEFT JOIN names n USING (cbsa_code)
        GROUP BY h.cbsa_code, n.cbsa_name, EXTRACT(YEAR FROM h.period)
    """).df()
    df["cbsa_name"] = df["cbsa_name"].fillna("?").astype(str)
    return df


def _quarterly_panel(con, since: str = "1985-01-01") -> pd.DataFrame:
    df = con.execute("""
        WITH names AS (
            SELECT DISTINCT cbsa_code, ANY_VALUE(cbsa_name) AS cbsa_name
            FROM county_cbsa_xwalk GROUP BY cbsa_code
        )
        SELECT h.cbsa_code, COALESCE(n.cbsa_name, '?') AS cbsa_name,
               h.period, h.hpi
        FROM fhfa_hpi_metro h
        LEFT JOIN names n USING (cbsa_code)
        WHERE h.period >= ?
        ORDER BY h.cbsa_code, h.period
    """, [since]).df()
    df["cbsa_name"] = df["cbsa_name"].fillna("?").astype(str)
    return df


def _cagr(start, end, years):
    if pd.isna(start) or pd.isna(end) or start <= 0 or years <= 0:
        return None
    try:
        return (end / start) ** (1.0 / years) - 1
    except (ValueError, ZeroDivisionError):
        return None


def _max_drawdown(series: pd.Series) -> Optional[float]:
    s = series.dropna()
    if len(s) < 4:
        return None
    running_max = s.cummax()
    return float(((s - running_max) / running_max).min())


def _time_to_recover(series: pd.Series) -> Optional[int]:
    s = series.dropna()
    if len(s) < 4:
        return None
    running_max = s.cummax()
    dd = (s - running_max) / running_max
    trough = dd.idxmin()
    if trough == s.index[0]:
        return 0
    pre = s.loc[:trough]
    peak_idx = pre.idxmax()
    peak_val = s.loc[peak_idx]
    after = s.loc[trough:]
    rec = after[after >= peak_val]
    if len(rec) == 0:
        return None
    return int((rec.index[0] - peak_idx).days // 30)


# ---- top-level analyses ----------------------------------------------------

def regime_decomposition(con) -> list[dict]:
    """For each regime: median / P10 / P90 metro CAGR + best + worst metro."""
    panel = _annual_hpi_panel(con)
    piv = panel.pivot(index=["cbsa_code", "cbsa_name"], columns="year", values="hpi")
    out = []
    for name, y0, y1 in REGIMES:
        years = y1 - y0
        rows = []
        for (cbsa, cn), r in piv.iterrows():
            c = _cagr(r.get(y0), r.get(y1), years)
            if c is not None:
                rows.append({"cbsa": cbsa, "name": cn, "cagr": c})
        if not rows:
            out.append({"regime": name, "years": f"{y0}-{y1}", "n": 0})
            continue
        s = pd.DataFrame(rows)
        s_sorted = s.sort_values("cagr", ascending=False)
        out.append({
            "regime":  name,
            "years":   f"{y0}-{y1}",
            "n":       len(s),
            "median_cagr":  round(float(s["cagr"].median()), 4),
            "p10_cagr":     round(float(s["cagr"].quantile(0.10)), 4),
            "p90_cagr":     round(float(s["cagr"].quantile(0.90)), 4),
            "best_metro":   s_sorted.iloc[0]["name"][:45],
            "best_cagr":    round(float(s_sorted.iloc[0]["cagr"]), 4),
            "worst_metro":  s_sorted.iloc[-1]["name"][:45],
            "worst_cagr":   round(float(s_sorted.iloc[-1]["cagr"]), 4),
        })
    return out


def drawdown_panel(con, top_n: int = 15) -> dict:
    """Worst-N + best-N drawdowns across all metros (quarterly data, 1985-).
    Returns dict with `worst`, `best`, and aggregate stats."""
    df = _quarterly_panel(con, since="1985-01-01")
    rows = []
    for cbsa, sub in df.groupby("cbsa_code"):
        s = sub.set_index("period")["hpi"]
        dd = _max_drawdown(s)
        if dd is None or len(s) < 100:
            continue
        ttr = _time_to_recover(s)
        rows.append({
            "cbsa": cbsa,
            "name": sub["cbsa_name"].iloc[0][:45],
            "max_dd_pct": round(dd * 100, 1),
            "ttr_months": ttr if ttr is not None else None,
        })
    ddf = pd.DataFrame(rows).sort_values("max_dd_pct")
    finite_ttr = ddf["ttr_months"].dropna()
    return {
        "n_metros":              len(ddf),
        "median_max_dd_pct":     round(float(ddf["max_dd_pct"].median()), 1),
        "mean_max_dd_pct":       round(float(ddf["max_dd_pct"].mean()), 1),
        "p10_max_dd_pct":        round(float(ddf["max_dd_pct"].quantile(0.10)), 1),
        "p90_max_dd_pct":        round(float(ddf["max_dd_pct"].quantile(0.90)), 1),
        "median_ttr_months":     int(finite_ttr.median()) if len(finite_ttr) else None,
        "p10_ttr_months":        int(finite_ttr.quantile(0.10)) if len(finite_ttr) else None,
        "p90_ttr_months":        int(finite_ttr.quantile(0.90)) if len(finite_ttr) else None,
        "never_recovered_count": int(ddf["ttr_months"].isna().sum()),
        "worst": ddf.head(top_n).to_dict("records"),
        "best":  ddf.tail(top_n).to_dict("records"),
    }


def momentum_persistence(con, window_years: int = 3) -> dict:
    """Quartile-to-quartile transition matrix: past-window → next-window."""
    panel = _annual_hpi_panel(con).sort_values(["cbsa_code", "year"])
    panel["hpi_lag"]  = panel.groupby("cbsa_code")["hpi"].shift(window_years)
    panel["hpi_lead"] = panel.groupby("cbsa_code")["hpi"].shift(-window_years)
    panel["ret_past"] = panel["hpi"]  / panel["hpi_lag"]  - 1
    panel["ret_fwd"]  = panel["hpi_lead"] / panel["hpi"]  - 1
    panel = panel.dropna(subset=["ret_past", "ret_fwd"])

    def _safe_q(s):
        try:
            return pd.qcut(s.rank(method="first"), 4, labels=[4, 3, 2, 1])
        except ValueError:
            return pd.Series([pd.NA] * len(s), index=s.index)

    panel["q_past"] = panel.groupby("year")["ret_past"].transform(_safe_q)
    panel["q_fwd"]  = panel.groupby("year")["ret_fwd"].transform(_safe_q)
    clean = panel.dropna(subset=["q_past", "q_fwd"]).copy()
    clean["q_past"] = clean["q_past"].astype(int)
    clean["q_fwd"]  = clean["q_fwd"].astype(int)

    # Transition matrix (row-normalized %)
    tx = (pd.crosstab(clean["q_past"], clean["q_fwd"], normalize="index") * 100)
    matrix = {}
    for row in tx.index:
        matrix[int(row)] = {int(col): round(float(tx.loc[row, col]), 1) for col in tx.columns}

    by_q = []
    for q in [1, 2, 3, 4]:
        sub = clean[clean.q_past == q]
        by_q.append({
            "past_quartile":     q,
            "n":                 int(len(sub)),
            "mean_fwd_return":   round(float(sub["ret_fwd"].mean()), 4),
            "median_fwd_return": round(float(sub["ret_fwd"].median()), 4),
        })

    return {
        "window_years":          window_years,
        "n_transitions":         int(len(clean)),
        "transition_matrix_pct": matrix,
        "fwd_returns_by_quartile": by_q,
        "p_top_stays_top":     matrix.get(1, {}).get(1, 0) / 100,
        "p_top_to_bottom":     matrix.get(1, {}).get(4, 0) / 100,
        "p_bottom_stays_bottom": matrix.get(4, {}).get(4, 0) / 100,
        "top_minus_bottom_fwd_return": round(
            by_q[0]["mean_fwd_return"] - by_q[3]["mean_fwd_return"], 4
        ),
    }


def strategy_backtest(con, buy_year: int = 1990, sell_year: int = 2024,
                      compositions: Optional[dict] = None) -> list[dict]:
    """For each strategy: equal-weight portfolio holding multiple, CAGR,
    max DD, time-to-recover."""
    compositions = compositions or STRATEGY_COMPOSITIONS
    panel = _annual_hpi_panel(con)
    piv = panel.pivot(index=["cbsa_code", "cbsa_name"], columns="year", values="hpi")
    quarterly = _quarterly_panel(con, since=f"{buy_year}-01-01")

    out = []
    for sname, codes in compositions.items():
        multiples = []
        for code in codes:
            mask = piv.index.get_level_values("cbsa_code") == code
            if not mask.any():
                continue
            row = piv[mask].iloc[0]
            buy, sell = row.get(buy_year), row.get(sell_year)
            if pd.notna(buy) and pd.notna(sell) and buy > 0:
                multiples.append(sell / buy)
        if not multiples:
            out.append({"strategy": sname, "error": "no data"})
            continue
        m = sum(multiples) / len(multiples)
        years = sell_year - buy_year
        cagr = m ** (1 / years) - 1

        # Equal-weight portfolio drawdown (normalize each metro to 100, average across time)
        sub = quarterly[quarterly["cbsa_code"].isin(codes)].copy()
        if not sub.empty:
            sub["hpi_norm"] = sub.groupby("cbsa_code")["hpi"].transform(
                lambda x: 100 * x / x.iloc[0]
            )
            port = sub.groupby("period")["hpi_norm"].mean()
            dd  = _max_drawdown(port)
            ttr = _time_to_recover(port)
        else:
            dd, ttr = None, None

        out.append({
            "strategy":         sname,
            "metros":           len(multiples),
            "buy_year":         buy_year,
            "sell_year":        sell_year,
            "holding_multiple": round(m, 2),
            "cagr":             round(cagr, 4),
            "max_dd_pct":       round(dd * 100, 1) if dd is not None else None,
            "time_to_recover_months": ttr,
        })
    out.sort(key=lambda r: -(r.get("cagr") or -99))
    return out


def rent_yield_panel(con, top_n: int = 15) -> dict:
    """Top metros by 2015-2024 total return (price + rent), gross yield."""
    sql = """
    WITH
    zori AS (
        SELECT zip,
               LAST(value ORDER BY period) AS zori_now,
               FIRST(value ORDER BY period) AS zori_2015
        FROM zillow_zori WHERE period >= '2015-01-01' GROUP BY zip
    ),
    zhvi AS (
        SELECT zip,
               LAST(value ORDER BY period) AS zhvi_now,
               FIRST(value ORDER BY period) AS zhvi_2015
        FROM zillow_zhvi WHERE period >= '2015-01-01' GROUP BY zip
    ),
    xref AS (
        SELECT z.zip, c.cbsa_code, c.cbsa_name
        FROM zip_county_xwalk z JOIN county_cbsa_xwalk c USING (fips_county)
    )
    SELECT xref.cbsa_code, xref.cbsa_name,
           COUNT(DISTINCT xref.zip) AS n_zips,
           MEDIAN(12.0 * zori_now / NULLIF(zhvi_now, 0)) AS yield_now,
           MEDIAN(12.0 * zori_2015 / NULLIF(zhvi_2015, 0)) AS yield_2015,
           MEDIAN(zhvi_now / NULLIF(zhvi_2015, 0) - 1) AS price_appr_9y,
           MEDIAN(zori_now / NULLIF(zori_2015, 0) - 1) AS rent_appr_9y
    FROM zori JOIN zhvi USING (zip) JOIN xref USING (zip)
    GROUP BY xref.cbsa_code, xref.cbsa_name
    HAVING n_zips >= 5
    """
    df = con.execute(sql).df()
    df["avg_yield"] = (df["yield_now"] + df["yield_2015"]) / 2
    df["total_ret_9y"] = df["price_appr_9y"] + df["avg_yield"] * 9
    df["total_cagr_9y"] = (1 + df["total_ret_9y"]) ** (1 / 9) - 1

    keep_cols = ["cbsa_code", "cbsa_name", "n_zips", "yield_now",
                  "price_appr_9y", "rent_appr_9y", "total_cagr_9y"]
    by_total = df.sort_values("total_cagr_9y", ascending=False).head(top_n)
    by_yield = df.sort_values("yield_now", ascending=False).head(top_n)
    by_growth = df.sort_values("price_appr_9y", ascending=False).head(top_n)
    corr = float(df[["yield_now", "price_appr_9y"]].corr().iloc[0, 1])

    def _scrub(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            d = {}
            for k, v in r.items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    d[k] = None
                else:
                    d[k] = v
            out.append(d)
        return out

    return {
        "n_metros":            len(df),
        "corr_yield_vs_growth": round(corr, 3),
        "top_total_return":    _scrub(by_total[keep_cols].to_dict("records")),
        "top_yield":           _scrub(by_yield[keep_cols].to_dict("records")),
        "top_growth":          _scrub(by_growth[keep_cols].to_dict("records")),
    }


def full_report(con) -> dict:
    """Run all five analyses and return a single JSON-friendly dict."""
    return {
        "regimes":     regime_decomposition(con),
        "drawdowns":   drawdown_panel(con),
        "momentum":    momentum_persistence(con),
        "strategies":  strategy_backtest(con),
        "rent_yield":  rent_yield_panel(con),
    }


# ---- per-MSA stability tier ------------------------------------------------
#
# Derived from FHFA HPI 1985-now. Each metro gets one of:
#   "Boring"     — historical max DD ≥ -6%  (Pittsburgh, Iowa metros)
#   "Standard"   — between -6% and -30%
#   "Volatile"   — -30% to -45%
#   "Boom-Bust"  — worse than -45% (Merced, Vegas, Modesto, Stockton, Cape Coral)
#
# Plus the actual max-DD %, time-to-recover months. Cached for the
# msa_score path — recomputing on every /api/msas call is wasteful.
# Built lazily.

_STABILITY_CACHE: dict[str, dict] = {}

def compute_stability_panel(con) -> dict[str, dict]:
    """Return {cbsa_code: {max_dd_pct, ttr_months, tier}} for every metro."""
    if _STABILITY_CACHE:
        return _STABILITY_CACHE
    df = _quarterly_panel(con, since="1985-01-01")
    out = {}
    for cbsa, sub in df.groupby("cbsa_code"):
        s = sub.set_index("period")["hpi"]
        dd = _max_drawdown(s)
        if dd is None or len(s) < 60:    # need ≥15 years
            continue
        ttr = _time_to_recover(s)
        pct = round(dd * 100, 1)
        if pct >= -6:
            tier = "Boring"
        elif pct >= -30:
            tier = "Standard"
        elif pct >= -45:
            tier = "Volatile"
        else:
            tier = "Boom-Bust"
        out[cbsa] = {"max_dd_pct": pct, "ttr_months": ttr, "tier": tier}
    _STABILITY_CACHE.update(out)
    return out


def stability_for(con, cbsa_code: str) -> Optional[dict]:
    """Return stability dict for a single CBSA, or None if no data."""
    return compute_stability_panel(con).get(str(cbsa_code))
