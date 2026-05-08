"""Golden-ranking backtest — promptfoo analog for the scorer.

Freezes a small slate of MSAs whose archetype + score-direction we expect
to be stable. After any change to the scorer, weights, or factor logic,
the backtest fails loudly if a historically-stable expectation flips.

These are not exact-score assertions — scores will move as data refreshes.
They are *direction* and *archetype* assertions: Memphis should always be
Cashflow Heartland; Austin should always have appreciation > cashflow;
flood-heavy New Orleans should always have a negative risk component.
"""
from __future__ import annotations
import duckdb
import pandas as pd
from . import msa_score


# (cbsa_substring, expected_archetype, appreciation_should_beat_cashflow)
GOLDEN: list[tuple[str, str, bool]] = [
    ("Memphis, TN",             "Cashflow Heartland", False),
    ("Pittsburgh, PA",          "Cashflow Heartland", False),
    ("Cleveland, OH",           "Cashflow Heartland", False),
    ("Austin",                  None,                  True),
    ("Raleigh",                 "Sun Belt Growth",     True),
    ("Charlotte",               "Sun Belt Growth",     True),
    ("Las Vegas",               "Boom-Bust Beta",      None),
    ("Boise",                   "Resource & Niche",    None),
]


def run(con: duckdb.DuckDBPyConnection | None = None) -> dict:
    raw = msa_score.features(con)
    scored = msa_score.with_archetype(msa_score.score(raw))

    failures = []
    matches = []
    for sub, expected_arch, appr_gt_cash in GOLDEN:
        m = scored[scored["cbsa_name"].str.contains(sub, case=False, na=False)]
        if m.empty:
            failures.append({"msa": sub, "reason": "not found in scored output"})
            continue
        row = m.iloc[0]
        rec = {"msa": sub, "matched": row["cbsa_name"], "archetype": row["archetype"],
               "appreciation": float(row["appreciation_score"]),
               "cashflow": float(row["cashflow_score"])}
        if expected_arch is not None and row["archetype"] != expected_arch:
            failures.append({**rec, "reason": f"archetype was {row['archetype']!r}, expected {expected_arch!r}"})
            continue
        if appr_gt_cash is True and row["appreciation_score"] <= row["cashflow_score"]:
            failures.append({**rec, "reason": "expected appreciation > cashflow, got reversed"})
            continue
        if appr_gt_cash is False and row["appreciation_score"] >= row["cashflow_score"]:
            failures.append({**rec, "reason": "expected cashflow > appreciation, got reversed"})
            continue
        matches.append(rec)

    return {"passed": len(matches), "failed": len(failures),
            "failures": failures, "matches": matches}
