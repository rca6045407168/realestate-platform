"""reip CLI.

Commands:
    reip init / ingest / status / refresh / freshness
    reip top                   — zip-level legacy ranking
    reip msa-rank              — Appreciation × Cashflow × Total per Table 5
    reip diff                  — movers vs. previous snapshot
    reip archetype <CBSA>      — classify into 5 archetypes
    reip alpha                 — property-level alpha overlay
    reip underwrite <args>     — pro forma + DSCR + IRR + sensitivity
    reip backtest              — golden-ranking regression test
    reip report                — self-contained HTML report
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
import click
import pandas as pd
from .store import connect, init as store_init
from . import (
    score as score_zip, msa_score, alpha as alpha_mod, underwriting,
    snapshots, freshness as fresh_mod, backtest as backtest_mod, report as report_mod,
    render, avm as avm_mod, remarks as remarks_mod, recommendation as rec_mod,
)
from .loaders import (
    zillow,
    redfin as redfin_market,
    irs_migration,
    census_permits,
    bls_qcew,
    hud_fmr,
    fema,
    fred,
    zip_xwalk,
    cbsa_xwalk,
    acs,
    fhfa_hpi,
    static_data,
    schools as schools_loader,
)


SOURCES = {
    "zip_xwalk":   zip_xwalk,
    "cbsa_xwalk":  cbsa_xwalk,
    "static_data": static_data,
    "zillow":      zillow,
    "redfin":      redfin_market,
    "irs":         irs_migration,
    "permits":     census_permits,
    "fema":        fema,
    "fred":        fred,
    "hud":         hud_fmr,
    "bls":         bls_qcew,
    "acs":         acs,
    "fhfa":        fhfa_hpi,
    "schools":     schools_loader,
}


@click.group()
def cli():
    pass


@cli.command()
def init():
    con = connect(); store_init(con)
    click.echo("Schema initialized.")


@cli.command()
@click.option("--only", multiple=True)
@click.option("--skip", multiple=True)
@click.option("--refresh", is_flag=True)
def ingest(only, skip, refresh):
    """Pull configured sources unconditionally."""
    con = connect(); store_init(con)
    targets = [s for s in SOURCES if (not only or s in only) and s not in skip]
    for name in targets:
        mod = SOURCES[name]
        click.echo(f"→ {name} …", nl=False)
        try:
            kwargs = {}
            if "refresh" in mod.load.__code__.co_varnames:
                kwargs["refresh"] = refresh
            n = mod.load(con, **kwargs)
            fresh_mod.stamp(con, name, n)
            click.echo(f" {n} rows")
        except Exception as e:
            click.echo(f" FAILED: {e}")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show which sources are stale without re-pulling")
def refresh(dry_run):
    """Smart refresh — only re-pull sources past their cadence.

    Cadence per source defined in freshness.py: weekly (Redfin), monthly
    (Zillow, permits), quarterly (BLS, FHFA), annual (IRS, ACS, HUD).
    """
    con = connect(); store_init(con)
    stale = fresh_mod.stale_sources(con)
    if not stale:
        click.echo("All sources fresh. Nothing to do.")
        return
    click.echo(f"Stale sources: {', '.join(stale)}")
    if dry_run:
        return
    for name in stale:
        if name not in SOURCES:
            continue
        mod = SOURCES[name]
        click.echo(f"→ refreshing {name} …", nl=False)
        try:
            kwargs = {}
            if "refresh" in mod.load.__code__.co_varnames:
                kwargs["refresh"] = True
            n = mod.load(con, **kwargs)
            fresh_mod.stamp(con, name, n)
            click.echo(f" {n} rows")
        except Exception as e:
            click.echo(f" FAILED: {e}")


@cli.command()
def freshness():
    """Show per-source freshness with cadence-aware staleness flags."""
    con = connect(); store_init(con)
    render.freshness_table(fresh_mod.status(con))


@cli.command()
def status():
    con = connect(); store_init(con)
    for table in [
        "zillow_zhvi", "zillow_zori", "redfin_market", "irs_migration",
        "census_permits", "bls_qcew", "hud_fmr", "fema_nfip", "fred_macro",
        "zip_county_xwalk", "county_cbsa_xwalk", "acs_county",
        "fhfa_hpi_metro", "saiz_elasticity", "wharton_wrluri",
        "property_tax_state", "redfin_listings", "property_alpha",
    ]:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            n = "?"
        click.echo(f"  {table:25s} {n}")


@cli.command()
@click.option("--top", "n", default=25)
@click.option("--out", type=click.Path(), default=None)
@click.option("--w-yield", default=0.4)
@click.option("--w-growth", default=0.4)
@click.option("--w-risk", default=0.2)
@click.option("--min-completeness", default=0.5)
def top(n, out, w_yield, w_growth, w_risk, min_completeness):
    """Zip-level legacy ranking (yield × growth × risk)."""
    con = connect()
    df = score_zip.features(con)
    if df.empty:
        click.echo("No data — run `reip ingest`."); sys.exit(1)
    ranked = score_zip.score(df, w_yield=w_yield, w_growth=w_growth, w_risk=w_risk)
    ranked = ranked[ranked["completeness"] >= min_completeness]
    if out:
        ranked.to_csv(out, index=False)
    cols = ["zip", "zhvi", "yoy_appreciation", "gross_yield", "dom",
            "net_agi_inflow", "permits_12mo", "completeness", "score"]
    click.echo(ranked[[c for c in cols if c in ranked.columns]].head(n).to_string(
        index=False, float_format=lambda x: f"{x:,.4f}"))


@cli.command("msa-rank")
@click.option("--top", "n", default=25)
@click.option("--blend", default=0.5, help="Weight on Appreciation in Total Return (default 0.5)")
@click.option("--archetype", default=None, help="Filter to one archetype")
@click.option("--out", type=click.Path(), default=None)
@click.option("--by", type=click.Choice(["total", "appreciation", "cashflow"]), default="total")
@click.option("--no-snapshot", is_flag=True, help="Don't write a ranking snapshot")
def msa_rank(n, blend, archetype, out, by, no_snapshot):
    """Rank MSAs per the framework's Table 5 weights."""
    con = connect()
    raw = msa_score.features(con)
    if raw.empty:
        click.echo("No MSA features — run `reip ingest` first."); sys.exit(1)
    scored = msa_score.with_archetype(msa_score.score(raw, blend_w_appr=blend))
    if not no_snapshot:
        snapshots.snapshot(con, scored)
    if archetype:
        scored = scored[scored["archetype"] == archetype]
    sort_col = {"total": "total_return_score", "appreciation": "appreciation_score",
                "cashflow": "cashflow_score"}[by]
    scored = scored.sort_values(sort_col, ascending=False)
    if out:
        scored.to_csv(out, index=False)
    title = f"MSA ranking (sort={by}, blend appr={blend})"
    if archetype:
        title += f" — {archetype} only"
    render.msa_table(scored.head(n), title=title)


@cli.command()
@click.option("--by", type=click.Choice(["total", "appreciation", "cashflow"]), default="total")
@click.option("--top", "n", default=20, help="Top N movers to show")
def diff(by, n):
    """Compare the latest MSA ranking to the previous snapshot.

    Shows the largest movers — regime changes are where alpha hides.
    Run `reip msa-rank` two or more times to populate snapshots.
    """
    con = connect()
    df = snapshots.diff(con, by=by, top_movers=n)
    if df.empty:
        click.echo("Need at least two snapshots — run `reip msa-rank` again later.")
        return
    render.diff_table(df, title=f"Top {n} movers ({by} score)")


@cli.command()
def backtest():
    """Run the golden-ranking regression test on the scorer."""
    con = connect()
    out = backtest_mod.run(con)
    if out["failed"] == 0:
        click.echo(f"backtest: {out['passed']}/{out['passed']} passed")
    else:
        click.echo(f"backtest: {out['passed']} passed, {out['failed']} FAILED")
        for f in out["failures"]:
            click.echo(f"  ✗ {f.get('msa')}: {f.get('reason')}")
        sys.exit(1)


@cli.command()
@click.option("--out", type=click.Path(), default="data/reip-report.html")
@click.option("--blend", default=0.5)
def report(out, blend):
    """Build a self-contained HTML report (interactive table + JS underwriting calculator)."""
    con = connect()
    raw = msa_score.features(con)
    if raw.empty:
        click.echo("No MSA features — run `reip ingest` first."); sys.exit(1)
    scored = msa_score.with_archetype(msa_score.score(raw, blend_w_appr=blend))
    p = report_mod.build(scored, out, con=con)
    click.echo(f"Wrote {p}  (open in your browser; no server needed)")


@cli.command()
@click.argument("cbsa_or_name")
def archetype(cbsa_or_name):
    """Show archetype + factors for one MSA."""
    con = connect()
    raw = msa_score.features(con)
    scored = msa_score.with_archetype(msa_score.score(raw))
    match = scored[(scored["cbsa_code"].astype(str) == cbsa_or_name) |
                   (scored["cbsa_name"].str.contains(cbsa_or_name, case=False, na=False))]
    if match.empty:
        click.echo(f"No MSA matching '{cbsa_or_name}'"); sys.exit(1)
    row = match.iloc[0].to_dict()
    click.echo(f"\n{row['cbsa_name']}  ({row['cbsa_code']})")
    click.echo(f"  archetype:           {row['archetype']}")
    click.echo(f"  population:          {row['pop']:,.0f}")
    click.echo(f"  5y pop CAGR:         {row['pop_cagr_5yr']*100:+.2f}%")
    click.echo(f"  5y emp CAGR:         {(row['emp_cagr_5yr'] or 0)*100:+.2f}%")
    click.echo(f"  net migration % pop: {(row['net_migration_pct_pop'] or 0)*100:+.2f}%")
    click.echo(f"  permits/1000 HH:     {row['permits_per_1000_hh'] or 0:.2f}")
    click.echo(f"  gross yield:         {(row['gross_yield'] or 0)*100:.2f}%")
    click.echo(f"  Saiz elasticity:     {row['elasticity']}")
    click.echo(f"  Wharton WRLURI:      {row['wrluri']}")
    click.echo(f"  property tax %:      {row['effective_property_tax']}")
    click.echo(f"\n  Appreciation Score:  {row['appreciation_score']:+.3f}")
    click.echo(f"  Cashflow Score:      {row['cashflow_score']:+.3f}")
    click.echo(f"  Total Return Score:  {row['total_return_score']:+.3f}")
    click.echo(f"  completeness:        {row['completeness']*100:.0f}%")


@cli.command()
@click.option("--out", type=click.Path(), default=None)
def alpha(out):
    """Compute property-level alpha overlay over redfin_listings."""
    con = connect()
    df = alpha_mod.compute(con)
    if df.empty:
        click.echo("No listings yet. Run the redfin_listings ingest with cookies."); sys.exit(1)
    n = alpha_mod.persist(con)
    click.echo(f"Stamped alpha on {n} listings.")
    if out:
        df.to_csv(out, index=False)
    click.echo(df.sort_values("alpha_stack", ascending=False).head(15).to_string(index=False))


@cli.command()
@click.option("--top", "n", default=25)
@click.option("--direction", type=click.Choice(["hot", "cold", "all"]), default="cold",
              help="hot = sales clearing above ZHVI; cold = below; all = both tails")
@click.option("--min-price", default=0, type=float)
@click.option("--max-price", default=2_000_000, type=float)
@click.option("--out", type=click.Path(), default=None)
def avm(n, direction, min_price, max_price, out):
    """Zip-level AVM mispricing signal (§5.7 Information alpha).

    Compares Zillow ZHVI (smoothed value index) to recent Redfin median
    sale price by zip. 'cold' zips = sales clearing below the index =
    buying opportunities; 'hot' zips = sales clearing above = momentum.
    """
    con = connect()
    avm_mod.persist(con)
    df = con.execute("SELECT * FROM zip_avm_signal WHERE zhvi BETWEEN ? AND ?",
                     [min_price, max_price]).df()
    if direction != "all":
        df = df[df["direction"] == direction]
    df = df.sort_values("divergence_z", ascending=(direction == "cold")).head(n)
    if out:
        df.to_csv(out, index=False)
    click.echo(df.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))


@cli.command()
@click.argument("text")
def remarks(text):
    """Parse free-text MLS remarks for behavioral / distressed signals."""
    sigs = remarks_mod.parse(text)
    click.echo(json.dumps({
        "motivated": sigs.motivated, "distressed": sigs.distressed,
        "use_change": sigs.use_change, "assumable": sigs.assumable,
        "price_cut": sigs.price_cut, "short_sale": sigs.short_sale,
        "probate": sigs.probate,
        "auction": sigs.auction,
        "score": round(sigs.score, 3),
        "matched_terms": list(sigs.matched_terms),
    }, indent=2))


@cli.command()
@click.option("--price", "purchase_price", required=True, type=float)
@click.option("--rehab", "rehab_cost", default=0.0, type=float)
@click.option("--arv", default=None, type=float)
@click.option("--rent", "monthly_rent", required=True, type=float)
@click.option("--rate", "mortgage_rate", default=0.07, type=float)
@click.option("--ltv", default=0.75, type=float)
@click.option("--vacancy", default=0.05, type=float)
@click.option("--exit-cap", default=0.06, type=float)
@click.option("--hold", "hold_years", default=5, type=int)
@click.option("--sensitivity", "show_sensitivity", is_flag=True)
def underwrite(purchase_price, rehab_cost, arv, monthly_rent, mortgage_rate,
               ltv, vacancy, exit_cap, hold_years, show_sensitivity):
    """Underwrite a single property: pro forma, DSCR, IRR, BRRRR refi.

    Example:
      reip underwrite --price 200000 --rehab 30000 --arv 280000 --rent 2200
    """
    a = underwriting.Assumptions(
        purchase_price=purchase_price, rehab_cost=rehab_cost, arv=arv,
        monthly_rent=monthly_rent, mortgage_rate=mortgage_rate, ltv=ltv,
        vacancy=vacancy, exit_cap=exit_cap, hold_years=hold_years,
    )
    out = underwriting.underwrite(a)
    click.echo(json.dumps(out, indent=2, default=str))
    if show_sensitivity:
        click.echo("\nSensitivity (rent × vacancy × exit cap → IRR / equity multiple):")
        click.echo(underwriting.sensitivity(a).to_string(index=False, float_format=lambda x: f"{x:.3f}"))


@cli.command()
@click.option("--out", type=click.Path(), default=None,
              help="Where to write the markdown digest. Default: ~/.reip/digest-<date>.md")
@click.option("--dry-run", is_flag=True, help="Print prompt + would-be cost; no LLM call.")
def digest(out, dry_run):
    """Generate a weekly market briefing with ONE Claude call.

    Designed to be the *only* automated LLM usage you need. Schedule
    this weekly via launchd / cron and skip ad-hoc chat to keep your
    Anthropic bill predictable.

    Output: a markdown brief covering: today's mortgage rate, top
    regime-adjusted IRR zips, any stale data sources, your saved
    pipeline summary, and a one-paragraph "what changed this week"
    synthesis.

    Cost: typically $0.02-0.05 per run. Weekly cadence ≈ $1-3/month.
    """
    import datetime
    from . import chat as chat_mod
    from . import strategy as strategy_mod
    from . import zip_returns
    from . import msa_score
    from .store import connect

    con = connect()
    today = datetime.date.today().isoformat()

    # ---- Build a tight context block (NO ad-hoc tool calls allowed) ----
    parts = [f"# Weekly real-estate briefing — {today}\n"]

    # Macro rates
    r = con.execute(
        "SELECT period, value FROM fred_macro WHERE series_id='MORTGAGE30US' "
        "ORDER BY period DESC LIMIT 1").fetchone()
    if r:
        parts.append(f"30Y mortgage rate: {r[1]:.2f}% (as of {r[0]})")

    # Top-5 regime-adjusted IRR zips
    try:
        raw = msa_score.features(con)
        archetypes = {}
        if not raw.empty:
            scored = msa_score.with_archetype(msa_score.score(raw))
            archetypes = dict(zip(scored["cbsa_code"].astype(str), scored["archetype"]))
        zips = zip_returns.rank_us(con, sort="regime", limit=5,
                                    min_price=80_000, max_price=400_000,
                                    archetypes_by_cbsa=archetypes)
        if zips:
            parts.append("\nTop 5 zips by regime-adjusted 5y IRR (this week):")
            for z in zips:
                parts.append(
                    f"  {z.zip} {z.state} {z.cbsa_name}: "
                    f"adjIRR {z.regime_adjusted_irr*100:+.1f}%  "
                    f"price ${z.typical_price:,.0f}  rent ${z.typical_rent:,.0f}/mo  "
                    f"regime={z.regime_label}"
                )
    except Exception:
        pass

    user_prompt = (
        "\n".join(parts) +
        "\n\nWrite a tight (≤300 words) weekly briefing for an active real-estate "
        "investor based on the data above. Cover: (1) current rate environment, "
        "(2) the most interesting zip on the list and why, (3) one risk to watch "
        "this week. Markdown. No preamble."
    )

    if dry_run:
        click.echo("=== Would send this prompt to Claude (no API call) ===")
        click.echo(user_prompt)
        click.echo()
        # Rough cost estimate: ~1K input + ~500 output tokens
        est_in = len(user_prompt) // 4
        est_out = 500
        cost = est_in * 3 / 1_000_000 + est_out * 15 / 1_000_000
        click.echo(f"Estimated cost: ${cost:.4f}")
        return

    # Single LLM call. No tools, no history. Cheapest possible round-trip.
    result = chat_mod.chat(user_message=user_prompt, history=None,
                            max_tool_iters=1, pipeline_summary=None)
    if "error" in result:
        click.echo(f"Digest failed: {result['error']}", err=True)
        raise SystemExit(1)

    body = result.get("reply", "")
    usage = result.get("usage", {})
    cost = (
        usage.get("input_tokens", 0) * 3 +
        usage.get("output_tokens", 0) * 15 +
        usage.get("cache_creation_input_tokens", 0) * 3.75 +
        usage.get("cache_read_input_tokens", 0) * 0.30
    ) / 1_000_000

    # Persist to disk
    from pathlib import Path
    if out is None:
        out_dir = Path.home() / ".reip"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"digest-{today}.md"
    Path(out).write_text(body + f"\n\n---\nCost: ${cost:.4f}\nGenerated by `reip digest`\n")
    click.echo(f"Digest written → {out}")
    click.echo(f"Cost this run: ${cost:.4f}")


@cli.command()
def mcp():
    """Start the reip MCP server on stdio.

    Exposes every chat tool (top_zips, top_msas, msa_detail, live_listings,
    underwrite, avm_zips, parse_remarks, buy_box, stress_test,
    strategy_backtest, portfolio_resilience, current_rates) over the
    Model Context Protocol. Wire into Claude Code / Cursor / Continue
    via their mcp.json. No LLM is invoked here — pure analytical
    dispatch, $0 API cost.

    Setup (Claude Code):
      Add to ~/.claude.json:
        {"mcpServers": {"reip": {"command": "<absolute-path>/reip", "args": ["mcp"]}}}

    Then in any Claude Code session: ask "use reip to stress test a
    $80k MO deal with $1700/mo rent" — the call goes to this server
    instead of the /api/chat endpoint, billing zero Anthropic credit.
    """
    from . import mcp_server
    mcp_server.run_stdio()


@cli.command()
def macro():
    """Show current macro rates from the local fred_macro table."""
    con = connect()
    for sid, label in [("MORTGAGE30US", "30Y mortgage"),
                        ("DGS10",         "10Y Treasury"),
                        ("FEDFUNDS",      "Fed funds")]:
        r = con.execute(
            "SELECT period, value FROM fred_macro WHERE series_id = ? "
            "ORDER BY period DESC LIMIT 1", [sid]
        ).fetchone()
        if r:
            click.echo(f"  {label:15s} {r[1]:6.2f}%  (as of {r[0]})")
        else:
            click.echo(f"  {label:15s} (no data — run `reip ingest --only fred`)")


@cli.group()
def strategy():
    """50-year empirical strategy analyses (see docs/STRATEGY.md)."""
    pass


@strategy.command("backtest")
@click.option("--section", type=click.Choice(["regimes", "drawdowns", "momentum",
                                                "strategies", "rent_yield", "all"]),
              default="all", help="Which analysis to run")
@click.option("--as-json", "as_json", is_flag=True, help="Emit raw JSON (default: pretty tables)")
def strategy_backtest(section, as_json):
    """Run the 50-year strategy backtests against current data.

    Default emits human-readable tables for each section. --as-json emits
    the raw payload for piping into other tools.
    """
    from . import strategy as strat
    con = connect()
    if section == "all":
        report = strat.full_report(con)
    else:
        fn = {"regimes":    strat.regime_decomposition,
              "drawdowns":  strat.drawdown_panel,
              "momentum":   strat.momentum_persistence,
              "strategies": strat.strategy_backtest,
              "rent_yield": strat.rent_yield_panel}[section]
        report = {section: fn(con)}
    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return
    if "regimes" in report:
        click.echo("\n=== REGIMES 1985-2024 ===")
        for r in report["regimes"]:
            if "median_cagr" not in r:
                continue
            click.echo(f"  {r['regime']:<26s} {r['years']}  n={r['n']}  "
                       f"median {r['median_cagr']*100:+5.1f}%  "
                       f"P10→P90 [{r['p10_cagr']*100:+.1f}%, {r['p90_cagr']*100:+.1f}%]  "
                       f"best: {r['best_metro'][:30]} ({r['best_cagr']*100:+.1f}%)")
    if "drawdowns" in report:
        d = report["drawdowns"]
        click.echo(f"\n=== DRAWDOWNS (n={d['n_metros']}) ===")
        click.echo(f"  median {d['median_max_dd_pct']:.1f}%   P10 worst {d['p10_max_dd_pct']:.1f}%   "
                   f"median TTR {d['median_ttr_months']/12:.1f}yr")
        click.echo("  worst 10:")
        for r in d["worst"][:10]:
            ttr = f"{r['ttr_months']/12:.1f}yr" if r.get('ttr_months') else "never"
            click.echo(f"    {r['name']:<40s} {r['max_dd_pct']:6.1f}%   {ttr}")
        click.echo("  shallowest 10 (boring tier):")
        for r in d["best"][:10]:
            ttr = f"{r['ttr_months']/12:.1f}yr" if r.get('ttr_months') else "never"
            click.echo(f"    {r['name']:<40s} {r['max_dd_pct']:6.1f}%   {ttr}")
    if "momentum" in report:
        m = report["momentum"]
        click.echo(f"\n=== MOMENTUM ({m['window_years']}y window, n={m['n_transitions']}) ===")
        click.echo(f"  P(top→top) {m['p_top_stays_top']*100:.1f}%   "
                   f"P(top→bottom) {m['p_top_to_bottom']*100:.1f}%   "
                   f"Q1−Q4 fwd edge: {m['top_minus_bottom_fwd_return']*100:+.1f}pp")
        for r in m["fwd_returns_by_quartile"]:
            click.echo(f"    Past-Q{r['past_quartile']} → fwd-{m['window_years']}y mean "
                       f"{r['mean_fwd_return']*100:+5.1f}%   median {r['median_fwd_return']*100:+5.1f}%   n={r['n']:,}")
    if "strategies" in report:
        click.echo("\n=== STRATEGIES (34y backtest) ===")
        for s in report["strategies"]:
            if "error" in s: continue
            click.echo(f"  {s['strategy']:<18s} {s['holding_multiple']:5.2f}x  "
                       f"CAGR {s['cagr']*100:+5.2f}%  DD {s['max_dd_pct']:.1f}%  "
                       f"TTR {(s['time_to_recover_months'] or 0)/12:.1f}yr")
    if "rent_yield" in report:
        ry = report["rent_yield"]
        click.echo(f"\n=== TOP 10 TOTAL RETURN 2015-2024 (n={ry['n_metros']}, yield-vs-growth corr={ry['corr_yield_vs_growth']:+.2f}) ===")
        for r in ry["top_total_return"][:10]:
            click.echo(f"  {r['cbsa_name']:<40s} yield {(r.get('yield_now') or 0)*100:5.2f}%   "
                       f"appr {(r.get('price_appr_9y') or 0)*100:+6.0f}%   "
                       f"total CAGR {(r.get('total_cagr_9y') or 0)*100:+5.1f}%")


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8787, type=int)
@click.option("--reload", is_flag=True)
def serve(host, port, reload):
    """Run the FastAPI backend + SPA. Open http://localhost:8787/."""
    import uvicorn
    click.echo(f"→ http://{host}:{port}/")
    uvicorn.run("reip.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
