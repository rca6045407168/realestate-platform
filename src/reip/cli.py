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
    snapshots, freshness as fresh_mod, backtest as backtest_mod, report as report_mod, render,
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
    p = report_mod.build(scored, out)
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


if __name__ == "__main__":
    cli()
