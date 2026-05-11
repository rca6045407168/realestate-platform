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

# Several large MSAs are subdivided into Metropolitan Divisions in the FHFA
# HPI series. zip_county_xwalk maps to the MSA code, but FHFA reports under
# the largest division code. Fall back to the division when the MSA itself
# isn't in fhfa_hpi_metro. (Mapping below: MSA → largest Division.)
_MSA_TO_FHFA_DIVISION = {
    "14460": "14454",   # Boston-Cambridge-Newton → Boston-Cambridge-Newton (Div)
    "16980": "16974",   # Chicago-Naperville-Elgin → Chicago-Naperville-Evanston (Div)
    "19100": "19124",   # Dallas-Fort Worth-Arlington → Dallas-Plano-Irving (Div)
    "19820": "19804",   # Detroit-Warren-Dearborn → Detroit-Dearborn-Livonia (Div)
    "31080": "31084",   # Los Angeles-Long Beach-Anaheim → LA-Long Beach-Glendale (Div)
    "33100": "33124",   # Miami-Fort Lauderdale-Pompano Beach → Miami-Miami Beach-Kendall (Div)
    "35620": "35614",   # New York-Newark-Jersey City → New York-Jersey City-White Plains (Div)
    "37980": "37964",   # Philadelphia-Camden-Wilmington → Philadelphia (Div)
    "41860": "41884",   # San Francisco-Oakland-Fremont → San Francisco-Oakland-Hayward (Div)
    "42660": "42644",   # Seattle-Tacoma-Bellevue → Seattle-Bellevue-Kent (Div)
    "47900": "47894",   # Washington-Arlington-Alexandria → DC-Arlington-Alexandria (Div)
    "12060": None,      # Atlanta — single, no division
    "26420": None,      # Houston — single
    "37100": None,      # Oxnard-Thousand Oaks-Ventura — single
}

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
    """Return stability dict for a CBSA, falling back to the FHFA Division
    code for split metros (SF, NYC, LA, Chicago, DC, Boston, etc)."""
    panel = compute_stability_panel(con)
    c = str(cbsa_code)
    if c in panel:
        return panel[c]
    # Try the FHFA division fallback
    div = _MSA_TO_FHFA_DIVISION.get(c)
    if div and div in panel:
        return panel[div]
    return None


# ---- pipeline resilience score --------------------------------------------
#
# Given a user's pipeline of saved deals, compute "what would have happened
# in 2007-2012 if you'd held this exact portfolio?" Uses FHFA HPI 1985-now
# stability per CBSA, weighted by deal equity.
#
# Inputs: list of deal dicts (as Portfolio.aggregate expects). Each carries
# inputs.zip (preferred), inputs.state, inputs.purchase_price, inputs.ltv.
#
# Outputs:
#   weighted_historical_max_dd_pct  — equity-weighted worst peak-to-trough
#   weighted_recovery_years         — equity-weighted time-to-recover
#   resilience_score                — 0-100 (100 = boring tier, 0 = full crash)
#   tier_distribution               — { tier: pct_of_equity }
#   peer_benchmark                  — what All-Weather would have delivered
#   gap_vs_benchmark                — DD difference

# All-Weather composition's empirical stability — equity-weighted average of
# the metros listed in STRATEGY_COMPOSITIONS["All-Weather"], computed once.
_ALL_WEATHER_BENCHMARK_CACHE: Optional[dict] = None


def _compute_all_weather_benchmark(con) -> dict:
    """Drawdown profile of the All-Weather reference portfolio."""
    global _ALL_WEATHER_BENCHMARK_CACHE
    if _ALL_WEATHER_BENCHMARK_CACHE is not None:
        return _ALL_WEATHER_BENCHMARK_CACHE
    panel = compute_stability_panel(con)
    rows = [panel[c] for c in ALL_WEATHER if c in panel]
    if not rows:
        _ALL_WEATHER_BENCHMARK_CACHE = {"max_dd_pct": None, "recovery_years": None}
        return _ALL_WEATHER_BENCHMARK_CACHE
    avg_dd = sum(r["max_dd_pct"] for r in rows) / len(rows)
    finite_ttr = [r["ttr_months"] for r in rows if r.get("ttr_months") is not None]
    avg_ttr_y = (sum(finite_ttr) / len(finite_ttr) / 12) if finite_ttr else None
    _ALL_WEATHER_BENCHMARK_CACHE = {
        "max_dd_pct":     round(avg_dd, 1),
        "recovery_years": round(avg_ttr_y, 1) if avg_ttr_y else None,
    }
    return _ALL_WEATHER_BENCHMARK_CACHE


def _deal_to_cbsa(con, deal: dict) -> Optional[str]:
    """Best-effort: get CBSA for a deal. Prefer zip → cbsa; fall back to None."""
    inp = deal.get("inputs") or {}
    z = inp.get("zip")
    if z:
        try:
            r = con.execute(
                "SELECT c.cbsa_code FROM zip_county_xwalk z LEFT JOIN county_cbsa_xwalk c "
                "USING (fips_county) WHERE z.zip = ? AND c.cbsa_code IS NOT NULL LIMIT 1",
                [str(z).zfill(5)],
            ).fetchone()
            if r and r[0]:
                return str(r[0])
        except Exception:
            pass
    return None


def _deal_equity(deal: dict) -> float:
    inp = deal.get("inputs") or {}
    price = float(inp.get("purchase_price") or 0)
    ltv = float(inp.get("ltv") or 0.75)
    rehab = float(inp.get("rehab_cost") or 0)
    return price * (1 - ltv) + price * 0.03 + rehab


def portfolio_resilience(con, deals: list[dict]) -> dict:
    """Compute the equity-weighted historical drawdown + recovery for a
    pipeline of deals, with a benchmark comparison."""
    panel = compute_stability_panel(con)
    bench = _compute_all_weather_benchmark(con)

    total_eq = 0.0
    weighted_dd = 0.0
    weighted_ttr_months = 0.0
    ttr_weight = 0.0
    tier_eq: dict[str, float] = {}
    mapped, unmapped = 0, 0
    per_deal = []
    for d in deals:
        eq = _deal_equity(d)
        if eq <= 0:
            continue
        total_eq += eq
        cbsa = _deal_to_cbsa(con, d)
        stab = stability_for(con, cbsa) if cbsa else None
        if not stab:
            unmapped += 1
            tier_eq["Unknown"] = tier_eq.get("Unknown", 0) + eq
            per_deal.append({
                "label": d.get("label"),
                "cbsa": None, "tier": None,
                "historical_max_dd_pct": None,
                "historical_recovery_years": None,
                "equity": round(eq, 2),
            })
            continue
        mapped += 1
        weighted_dd += stab["max_dd_pct"] * eq
        if stab.get("ttr_months") is not None:
            weighted_ttr_months += stab["ttr_months"] * eq
            ttr_weight += eq
        tier_eq[stab["tier"]] = tier_eq.get(stab["tier"], 0) + eq
        per_deal.append({
            "label": d.get("label"),
            "cbsa": cbsa, "tier": stab["tier"],
            "historical_max_dd_pct": stab["max_dd_pct"],
            "historical_recovery_years": round(stab["ttr_months"]/12, 1) if stab.get("ttr_months") else None,
            "equity": round(eq, 2),
        })

    if total_eq <= 0:
        return {"resilience_score": None, "error": "no equity"}

    weighted_dd_pct = weighted_dd / total_eq if mapped else None
    weighted_recovery_y = (weighted_ttr_months / ttr_weight / 12) if ttr_weight else None

    # Score: linearly map weighted DD from -65% (worst observed = 0) to 0% (perfect = 100)
    if weighted_dd_pct is not None:
        score = max(0, min(100, round((weighted_dd_pct + 65) / 65 * 100)))
    else:
        score = None

    # Tier distribution as percentages
    tier_dist = {t: round(e / total_eq, 4) for t, e in tier_eq.items()}

    # Gap vs benchmark
    gap = (weighted_dd_pct - bench["max_dd_pct"]) if (
        weighted_dd_pct is not None and bench.get("max_dd_pct") is not None) else None

    return {
        "resilience_score":              score,
        "weighted_historical_max_dd_pct": round(weighted_dd_pct, 1) if weighted_dd_pct is not None else None,
        "weighted_recovery_years":       round(weighted_recovery_y, 1) if weighted_recovery_y else None,
        "deals_mapped":                  mapped,
        "deals_unmapped":                unmapped,
        "tier_distribution_by_equity":   tier_dist,
        "per_deal":                      per_deal,
        "benchmark":                     {"name": "All-Weather Lifestyle", **bench},
        "gap_vs_benchmark_dd_pct":       round(gap, 1) if gap is not None else None,
        "interpretation":                _resilience_interp(score, weighted_dd_pct, weighted_recovery_y, gap),
    }


def _resilience_interp(score, dd_pct, recovery_y, gap) -> str:
    """One-paragraph plain-English interpretation."""
    if score is None:
        return "Not enough mapped deals to score this portfolio."
    if score >= 80:
        base = "Resilient. Historical worst-case for your composition is shallow."
    elif score >= 60:
        base = "Standard exposure. You'd absorb a typical recession drawdown."
    elif score >= 40:
        base = "Elevated risk. Historical equivalent took years to recover."
    else:
        base = "Fragile. This composition matches the metros that crashed hardest 2007-2012."
    detail = ""
    if dd_pct is not None:
        detail += f" Equity-weighted historical max drawdown: {dd_pct:.1f}%."
    if recovery_y:
        detail += f" Time-to-recover: {recovery_y:.1f} years."
    if gap is not None:
        # gap = our_DD - benchmark_DD. Both are negative (drawdowns).
        # More negative gap = worse than benchmark. Less negative = better.
        if gap > 5:
            detail += f" Beats the All-Weather benchmark by {gap:.1f}pp."
        elif gap < -5:
            detail += f" Trails the All-Weather benchmark by {abs(gap):.1f}pp deeper drawdown."
    return base + detail
