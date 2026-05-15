"""Decision ledger — Richard's verdicts as fast-weight context.

Inspired by Tiwari et al. 2026, "Learning, Fast and Slow: Towards LLMs
That Adapt Continually" (arXiv:2605.12484). The paper argues that fast
weights (optimized context) can absorb task-specific signal while slow
weights (model parameters / empirical defaults) stay conservative.

In reip's mapping:
  - SLOW: stress.py thresholds, _STATE_OVERLAYS, FHFA HPI panels, the
    50y empirical defaults. Never tuned to make a deal pass.
  - FAST: per-user buy-box context + this decision ledger, prepended to
    the cached system prompt. Updates session-to-session.

The ledger captures "Richard said BUY/PASS/WATCH on <zip> because <X>"
so future chat turns see his revealed preferences without anyone editing
scoring formulas. Read on chat init; written via the record_decision
tool during conversation.

Storage: ~/.reip/decisions.jsonl. One line per decision, append-only.
"""
from __future__ import annotations
import datetime
import json
from pathlib import Path
from typing import Optional


_LOG = Path.home() / ".reip" / "decisions.jsonl"
_VALID_VERDICTS = {"BUY", "PASS", "WATCH"}


def _path() -> Path:
    """Return the ledger path. Function-scoped so tests can monkeypatch."""
    return _LOG


def append(zip_code: str,
           verdict: str,
           reason: str,
           action: Optional[str] = None,
           extra: Optional[dict] = None) -> dict:
    """Record one decision. Returns the written record (for echo to the LLM).

    `verdict` must be one of BUY / PASS / WATCH. We reject unknown verdicts
    rather than coercing — keeping the schema tight so downstream readers
    can rely on the enum.
    """
    v = (verdict or "").strip().upper()
    if v not in _VALID_VERDICTS:
        return {"error": f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {verdict!r}"}
    z = (zip_code or "").strip()
    if not z:
        return {"error": "zip is required"}
    r = (reason or "").strip()
    if not r:
        return {"error": "reason is required (short plain-English rationale)"}

    rec: dict = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "zip": z,
        "verdict": v,
        "reason": r[:400],   # cap so a runaway reason can't blow context
    }
    if action:
        rec["action"] = action[:200]
    if extra and isinstance(extra, dict):
        # Allow callers to capture deal context (price, state, msa, etc.)
        # Keys are filtered to known-safe scalars to avoid prompt bloat.
        for k in ("price", "state", "msa", "irr", "verdict_gate"):
            if k in extra and extra[k] is not None:
                rec[k] = extra[k]

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def recent(limit: int = 20) -> list[dict]:
    """Return the last N decisions, newest first. Reads only the tail of
    the file (linear scan is fine — JSONL is small for a personal ledger).
    """
    p = _path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        # Tail-read: load all lines (file is small) and slice.
        lines = p.read_text().splitlines()
        for line in reversed(lines[-max(limit, 1) * 4:]):  # over-read in case of bad lines
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= limit:
                break
    except Exception:
        return []
    return out


def render_context_block(limit: int = 10) -> str:
    """Render the recent decisions as a system-prompt block. Empty string
    if no decisions yet — keeps the prompt identical for new users so the
    cache key stays stable.
    """
    rows = recent(limit)
    if not rows:
        return ""
    lines = [f"\n## Richard's recent decisions (most-recent first — fast-weight context):"]
    for r in rows:
        bits = [f"  - [{r.get('verdict', '?')}] {r.get('zip', '?')}"]
        if r.get("state"):
            bits.append(f"({r['state']})")
        if r.get("price"):
            try:
                bits.append(f"@ ${int(r['price']):,}")
            except (TypeError, ValueError):
                pass
        if r.get("verdict_gate"):
            bits.append(f"gate={r['verdict_gate']}")
        bits.append(f"— {r.get('reason', '')}")
        lines.append(" ".join(bits))
    lines.append(
        "(These are Richard's revealed preferences. Weight them when ranking "
        "or recommending; do NOT soften the stress-gate thresholds because of them.)"
    )
    return "\n".join(lines)
