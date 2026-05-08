"""reip CLI.

Commands:
    reip init         — initialize duckdb schema
    reip ingest       — pull all configured public datasets
    reip top [N]      — rank zips by composite score, write CSV
    reip status       — show row counts per table
"""
from __future__ import annotations
import sys
from pathlib import Path
import click
from .store import connect, init as store_init
from . import score as score_mod
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
)


SOURCES = {
    "zip_xwalk": zip_xwalk,        # must be first — others reference it
    "zillow": zillow,
    "redfin": redfin_market,
    "irs": irs_migration,
    "permits": census_permits,
    "fema": fema,
    "fred": fred,
    "hud": hud_fmr,
    "bls": bls_qcew,               # last — depends on xwalk to know which counties
}


@click.group()
def cli():
    pass


@cli.command()
def init():
    """Create duckdb tables if they don't exist."""
    con = connect()
    store_init(con)
    click.echo("Schema initialized.")


@cli.command()
@click.option("--only", multiple=True, help="Run only these sources (e.g. --only zillow --only redfin)")
@click.option("--skip", multiple=True, help="Skip these sources")
@click.option("--refresh", is_flag=True, help="Bypass HTTP cache")
def ingest(only, skip, refresh):
    """Pull all configured datasets into the duckdb store."""
    con = connect()
    store_init(con)
    targets = [s for s in SOURCES if (not only or s in only) and s not in skip]
    for name in targets:
        mod = SOURCES[name]
        click.echo(f"→ {name} …", nl=False)
        try:
            kwargs = {}
            if "refresh" in mod.load.__code__.co_varnames:
                kwargs["refresh"] = refresh
            n = mod.load(con, **kwargs)
            click.echo(f" {n} rows")
        except Exception as e:
            click.echo(f" FAILED: {e}")
    click.echo("\nDone. `reip status` for counts, `reip top` for rankings.")


@cli.command()
def status():
    """Show row counts per table."""
    con = connect()
    store_init(con)
    for table in [
        "zillow_zhvi", "zillow_zori", "redfin_market", "irs_migration",
        "census_permits", "bls_qcew", "hud_fmr", "fema_nfip", "fred_macro",
        "zip_county_xwalk", "redfin_listings",
    ]:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            n = "?"
        click.echo(f"  {table:25s} {n}")


@cli.command()
@click.option("--top", "n", default=25, help="Show top N zips")
@click.option("--state", default=None, help="Filter to a state (e.g. CA, TX)")
@click.option("--out", type=click.Path(), default=None, help="Write full ranked CSV to this path")
@click.option("--w-yield", default=0.4)
@click.option("--w-growth", default=0.4)
@click.option("--w-risk", default=0.2)
@click.option("--min-completeness", default=0.5,
              help="Only show zips with this fraction of input signals present")
def top(n, state, out, w_yield, w_growth, w_risk, min_completeness):
    """Rank zips by yield × growth × risk composite. Defaults: 0.4/0.4/0.2."""
    con = connect()
    df = score_mod.features(con)
    if df.empty:
        click.echo("No data yet — run `reip ingest`.")
        sys.exit(1)
    ranked = score_mod.score(df, w_yield=w_yield, w_growth=w_growth, w_risk=w_risk)
    ranked = ranked[ranked["completeness"] >= min_completeness]
    if out:
        ranked.to_csv(out, index=False)
        click.echo(f"Wrote {len(ranked)} rows → {out}")
    cols = ["zip", "zhvi", "yoy_appreciation", "gross_yield", "dom",
            "net_agi_inflow", "permits_12mo", "flood_claims_total",
            "completeness", "score"]
    have = [c for c in cols if c in ranked.columns]
    pd_repr = ranked[have].head(n).to_string(index=False, float_format=lambda x: f"{x:,.4f}")
    click.echo(pd_repr)


if __name__ == "__main__":
    cli()
