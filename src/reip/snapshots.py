"""MSA-ranking snapshots and diff.

Take a snapshot after every score; the next run can show movers.
This is the alpha-tracking analog of a diff-viewer: regime changes are
where the alpha is.
"""
from __future__ import annotations
from datetime import datetime
import pandas as pd
import duckdb
from .store import upsert_df


def snapshot(con: duckdb.DuckDBPyConnection, scored: pd.DataFrame, ts: datetime | None = None) -> int:
    if scored.empty:
        return 0
    ts = ts or datetime.now()
    df = scored.copy()
    df["snapshot_at"] = ts
    df["total_rank"] = df["total_return_score"].rank(method="min", ascending=False).astype(int)
    df["appreciation_rank"] = df["appreciation_score"].rank(method="min", ascending=False).astype(int)
    df["cashflow_rank"] = df["cashflow_score"].rank(method="min", ascending=False).astype(int)
    cols = ["snapshot_at", "cbsa_code", "cbsa_name", "archetype",
            "appreciation_score", "cashflow_score", "total_return_score",
            "total_rank", "appreciation_rank", "cashflow_rank"]
    return upsert_df(con, "msa_rank_snapshots", df[cols])


def diff(con: duckdb.DuckDBPyConnection, by: str = "total", top_movers: int = 25) -> pd.DataFrame:
    """Compare the latest snapshot to the previous one."""
    score_col = {"total": "total_return_score",
                 "appreciation": "appreciation_score",
                 "cashflow": "cashflow_score"}[by]
    rank_col = {"total": "total_rank",
                "appreciation": "appreciation_rank",
                "cashflow": "cashflow_rank"}[by]
    snaps = con.execute("SELECT DISTINCT snapshot_at FROM msa_rank_snapshots ORDER BY snapshot_at DESC").df()
    if len(snaps) < 2:
        return pd.DataFrame()
    now_ts, prev_ts = snaps["snapshot_at"].iloc[0], snaps["snapshot_at"].iloc[1]
    sql = f"""
    WITH n AS (
        SELECT cbsa_code, cbsa_name, archetype, {score_col} AS score, {rank_col} AS rank
        FROM msa_rank_snapshots WHERE snapshot_at = ?
    ),
    p AS (
        SELECT cbsa_code, {score_col} AS score, {rank_col} AS rank
        FROM msa_rank_snapshots WHERE snapshot_at = ?
    )
    SELECT n.cbsa_code, n.cbsa_name, n.archetype,
           n.rank AS rank_now, p.rank AS rank_then,
           n.score AS score_now, p.score AS score_then,
           ABS(p.rank - n.rank) AS move
    FROM n JOIN p USING (cbsa_code)
    WHERE n.rank IS NOT NULL AND p.rank IS NOT NULL
    ORDER BY move DESC, ABS(n.score - p.score) DESC
    LIMIT ?
    """
    return con.execute(sql, [now_ts, prev_ts, top_movers]).df()
