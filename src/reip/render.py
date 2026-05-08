"""Output rendering — rich CLI tables, freshness badges, sparklines.

Inspired by VSCode's Shiki — drop in nicer rendering without changing the
underlying data flow.
"""
from __future__ import annotations
from datetime import datetime
import math
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def _spark(values: list[float]) -> str:
    """Compress a small numeric series into a unicode sparkline."""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    finite = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not finite:
        return ""
    lo, hi = min(finite), max(finite)
    span = hi - lo or 1
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(" ")
        else:
            idx = int((v - lo) / span * (len(blocks) - 1))
            out.append(blocks[idx])
    return "".join(out)


def _color_score(s: float) -> str:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return "[dim]—[/dim]"
    if s >= 0.20:
        return f"[bold green]{s:+.3f}[/bold green]"
    if s >= 0.05:
        return f"[green]{s:+.3f}[/green]"
    if s <= -0.20:
        return f"[bold red]{s:+.3f}[/bold red]"
    if s <= -0.05:
        return f"[red]{s:+.3f}[/red]"
    return f"[dim]{s:+.3f}[/dim]"


def _bar(pct: float, width: int = 10) -> str:
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return "—"
    n = int(round(pct * width))
    return "█" * n + "░" * (width - n)


ARCHETYPE_COLOR = {
    "Coastal Gateway":     "cyan",
    "Sun Belt Growth":     "green",
    "Cashflow Heartland":  "yellow",
    "Boom-Bust Beta":      "magenta",
    "Resource & Niche":    "blue",
    "Mixed":               "white",
}


def msa_table(df: pd.DataFrame, title: str = "MSA ranking") -> None:
    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("CBSA", justify="right", style="dim")
    t.add_column("MSA", overflow="fold", max_width=32)
    t.add_column("Archetype")
    t.add_column("Pop", justify="right")
    t.add_column("PopΔ5y", justify="right")
    t.add_column("Mig%", justify="right")
    t.add_column("Yield", justify="right")
    t.add_column("Permits/1k", justify="right")
    t.add_column("Appr", justify="right")
    t.add_column("Cash", justify="right")
    t.add_column("Total", justify="right")
    t.add_column("Cmp", justify="right")
    for _, r in df.iterrows():
        arch = r.get("archetype") or "—"
        color = ARCHETYPE_COLOR.get(arch, "white")
        t.add_row(
            str(r.get("cbsa_code", "") or ""),
            str(r.get("cbsa_name", "") or ""),
            f"[{color}]{arch}[/{color}]",
            f"{r.get('pop', 0):,.0f}" if pd.notna(r.get("pop")) else "—",
            f"{(r.get('pop_cagr_5yr') or 0) * 100:+.2f}%" if pd.notna(r.get("pop_cagr_5yr")) else "—",
            f"{(r.get('net_migration_pct_pop') or 0) * 100:+.2f}%" if pd.notna(r.get("net_migration_pct_pop")) else "—",
            f"{(r.get('gross_yield') or 0) * 100:.2f}%" if pd.notna(r.get("gross_yield")) else "—",
            f"{r.get('permits_per_1000_hh', 0):.1f}" if pd.notna(r.get("permits_per_1000_hh")) else "—",
            _color_score(r.get("appreciation_score")),
            _color_score(r.get("cashflow_score")),
            _color_score(r.get("total_return_score")),
            _bar(r.get("completeness")),
        )
    console.print(t)


def diff_table(diff: pd.DataFrame, title: str = "Ranking diff vs. previous snapshot") -> None:
    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("CBSA", style="dim")
    t.add_column("MSA", overflow="fold", max_width=34)
    t.add_column("Archetype")
    t.add_column("Rank now", justify="right")
    t.add_column("Rank then", justify="right")
    t.add_column("Move", justify="right")
    t.add_column("Score now", justify="right")
    t.add_column("ΔScore", justify="right")
    for _, r in diff.iterrows():
        move = r["rank_then"] - r["rank_now"]
        move_str = (f"[bold green]▲ {move}[/bold green]" if move > 0
                    else f"[bold red]▼ {-move}[/bold red]" if move < 0
                    else "[dim]·[/dim]")
        d = r["score_now"] - r["score_then"]
        delta = f"[green]+{d:.3f}[/green]" if d > 0 else f"[red]{d:.3f}[/red]" if d < 0 else "[dim]·[/dim]"
        arch = r.get("archetype") or "—"
        color = ARCHETYPE_COLOR.get(arch, "white")
        t.add_row(
            str(r["cbsa_code"]),
            str(r["cbsa_name"] or ""),
            f"[{color}]{arch}[/{color}]",
            str(int(r["rank_now"])),
            str(int(r["rank_then"])),
            move_str,
            f"{r['score_now']:+.3f}",
            delta,
        )
    console.print(t)


def freshness_table(rows: list[dict]) -> None:
    """Show source-by-source data freshness with cadence-aware staleness flag."""
    t = Table(title="Data freshness", box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("Source")
    t.add_column("Last refresh")
    t.add_column("Age (days)", justify="right")
    t.add_column("Expected cadence", justify="right")
    t.add_column("Status")
    t.add_column("Rows loaded", justify="right")
    now = datetime.now()
    for r in rows:
        last = r.get("last_refresh")
        age = (now - last).days if last else None
        cadence = r.get("expected_cadence_days") or 30
        if age is None:
            status = "[red]never[/red]"
        elif age > cadence * 2:
            status = "[bold red]VERY STALE[/bold red]"
        elif age > cadence:
            status = "[yellow]stale[/yellow]"
        else:
            status = "[green]fresh[/green]"
        t.add_row(
            r["source_name"],
            last.strftime("%Y-%m-%d") if last else "—",
            str(age) if age is not None else "—",
            f"{cadence}d",
            status,
            f"{r.get('rows_loaded') or 0:,}",
        )
    console.print(t)
