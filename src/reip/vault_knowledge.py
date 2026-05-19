"""Obsidian vault knowledge wiring.

Reads Richard's real-estate notes from his Obsidian vault and surfaces
them to the chat agent in two complementary ways:

  1. `load_knowledge_block()` — concatenates every .md under
     `<vault>/Real Estate Platform/Knowledge/` (except files starting
     with `_`) into a single markdown block. Called by chat._build_context
     at chat-init time so the content sits inside the cached system
     prefix. Cost: ~$0 on cache reads.

  2. `search(query, limit)` — filesystem grep across the whole vault.
     Wired as the `vault_search` chat tool. On-demand retrieval for
     "what did I write about <topic>" questions. Returns slim hits
     (path + 200-char excerpt) so tool results stay cheap.

Every `search()` call appends a row to `~/.reip/vault_search_log.jsonl`
({ts, query, tokens, n_hits, top_paths}). The intent (per MST-062 +
EvolveMem 2026-05-16): MEASURE retrieval quality before tuning weights.
30 days from now we'll know real call volume + hit-set characteristics;
only then is it worth touching the ranking. `search_log_summary()` /
the `/api/vault_search/stats` endpoint expose the aggregates.

Pattern follows decision_ledger.py — small, stdlib-only, monkeypatchable
for tests. Authoring stays in Obsidian; the platform never writes to
the vault from this module (read-only).
"""
from __future__ import annotations
import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Optional


# Defaults are overridable via env for tests + non-default vault locations.
_DEFAULT_VAULT = Path.home() / "Documents" / "Obsidian Vault"
# Vault reorganized 2026-05-17/18: `Real Estate Platform/` → `Ventures/Real Estate Platform/`.
# We probe both so a vault reorg can't silently empty the knowledge block;
# the new path wins when both exist.
_KNOWLEDGE_SUBDIRS = (
    "Ventures/Real Estate Platform/Knowledge",
    "Real Estate Platform/Knowledge",  # legacy — pre 2026-05-17
)
_KNOWLEDGE_SUBDIR = _KNOWLEDGE_SUBDIRS[0]  # kept for callers that import it

# Caps — keep the cached system block from runaway growth.
# Total budget is ~5K tokens of knowledge in the cached prefix; with the
# 90% cache-read discount that's ~$0.001 per chat call. Worth it.
_KNOWLEDGE_MAX_CHARS = 20_000     # ~5K tokens
_PER_FILE_MAX_CHARS  = 10_000     # one giant file can't crowd out everything else
_SEARCH_EXCERPT_CHARS = 200
_SEARCH_MAX_LIMIT = 20

# Frontmatter block at the top of a .md file. We strip it for the system
# prompt — the YAML metadata is for Obsidian, not the LLM.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _vault_root() -> Path:
    """Vault path. Env override for tests + alternate setups."""
    override = os.getenv("REIP_VAULT_PATH")
    if override:
        return Path(override)
    return _DEFAULT_VAULT


def _knowledge_dir() -> Path:
    """Return the first existing knowledge dir from the probe list. If
    none exists, return the canonical (new-path) location — empty-folder
    case is handled downstream and produces an empty block, not a crash."""
    root = _vault_root()
    for sub in _KNOWLEDGE_SUBDIRS:
        p = root / sub
        if p.is_dir():
            return p
    return root / _KNOWLEDGE_SUBDIRS[0]


# ---------------------------------------------------------------------------
# Layer 1 — knowledge block (cached into system prompt)
# ---------------------------------------------------------------------------

def load_knowledge_block() -> str:
    """Concatenate every visible .md file in the Knowledge/ folder into one
    block suitable for prepending to the system prompt.

    Returns "" when the folder is missing or empty — keeps the system
    prompt identical for new users so the cache key stays stable.
    """
    d = _knowledge_dir()
    if not d.is_dir():
        return ""

    files = sorted(
        (p for p in d.glob("*.md")
         if p.is_file() and not p.name.startswith("_")),
        key=lambda p: p.name.lower(),  # case-insensitive — "framework" sorts before "PLATFORM"
    )
    if not files:
        return ""

    parts = ["\n## Knowledge base (from Obsidian — Real Estate Platform/Knowledge/):"]
    total = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            parts.append(f"\n### {p.name}\n(could not read: {type(e).__name__})")
            continue
        # Strip YAML frontmatter; it's Obsidian-only metadata
        text = _FRONTMATTER_RE.sub("", text, count=1).strip()
        if len(text) > _PER_FILE_MAX_CHARS:
            text = text[:_PER_FILE_MAX_CHARS] + "\n[... truncated, see vault for full text]"
        block = f"\n### {p.stem}\n{text}\n"
        if total + len(block) > _KNOWLEDGE_MAX_CHARS:
            parts.append("\n[knowledge block hit 12 KB cap; remaining files skipped]")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Layer 2 — on-demand vault search (`vault_search` chat tool)
# ---------------------------------------------------------------------------

def search(query: str, limit: int = 5) -> list[dict]:
    """Find markdown notes in the vault matching `query` (case-insensitive
    substring). Returns up to `limit` hits, slim shape:

        [{path: "Real Estate Platform/reip.md", line: 12,
          excerpt: "...200-char window around the match..."}, ...]

    Errors return an empty list rather than raising — keeps the chat tool
    response shape stable.
    """
    q = (query or "").strip()
    if not q:
        return [{"error": "query is required"}]
    try:
        n = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
    except (TypeError, ValueError):
        n = 5

    root = _vault_root()
    if not root.is_dir():
        return []

    needle = q.lower()
    # Tokenize the query. Multi-word queries like "v5 spec pricing"
    # become a token set; a note matches iff ALL tokens appear in its
    # text. Single-word queries collapse to substring-match (same as before).
    tokens = [t for t in re.split(r"\W+", needle) if len(t) >= 2]
    # If query has no usable tokens (e.g. all 1-char or punctuation),
    # fall back to substring on the raw needle so the user still gets
    # results from quotes / punctuation-heavy queries.
    require_all_tokens = len(tokens) >= 2

    candidates: list[tuple[int, float, dict]] = []
    # Walk the whole vault but skip noisy / system folders.
    for path in root.rglob("*.md"):
        # Skip vendored / system folders + symlinked skill copies.
        if any(part.startswith(".") or part in {"node_modules", ".obsidian"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        text_lower = text.lower()
        if require_all_tokens:
            if not all(t in text_lower for t in tokens):
                continue
            # Anchor excerpt on the rarest / longest token's first hit
            anchor = max(tokens, key=len)
        else:
            if needle not in text_lower:
                continue
            anchor = needle
        idx = text_lower.find(anchor)
        start = max(0, idx - _SEARCH_EXCERPT_CHARS // 2)
        end = min(len(text), idx + _SEARCH_EXCERPT_CHARS // 2)
        excerpt = text[start:end].replace("\n", " ").strip()
        line_num = text[:idx].count("\n") + 1
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        # Relevance score: path-token matches dominate, recency tiebreaks.
        # Lower score sorts first (we negate matches so more matches → smaller).
        path_lower = str(rel).lower()
        path_match_score = -sum(1 for t in tokens if t in path_lower)
        try:
            mtime = -path.stat().st_mtime  # newer = smaller = sorts first
        except OSError:
            mtime = 0.0
        candidates.append((
            path_match_score, mtime,
            {"path": str(rel), "line": line_num, "excerpt": excerpt},
        ))
    candidates.sort(key=lambda c: (c[0], c[1]))
    results = [c[2] for c in candidates[:n]]
    _log_search(q, tokens, results)
    return results


# ---------------------------------------------------------------------------
# Outcome logging — measure-first before tuning ranking weights
# ---------------------------------------------------------------------------

_SEARCH_LOG = Path.home() / ".reip" / "vault_search_log.jsonl"


def _log_search_path() -> Path:
    """Function-scoped so tests can monkeypatch."""
    return _SEARCH_LOG


def _log_search(query: str, tokens: list[str], results: list[dict]) -> None:
    """Append one row per search call. Best-effort — errors swallowed
    so logging can never break the retrieval contract."""
    try:
        p = _log_search_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": _dt.datetime.utcnow().isoformat() + "Z",
            "query": query[:200],
            "tokens": tokens[:10],
            "n_hits": len(results),
            "top_paths": [r.get("path") for r in results[:5] if isinstance(r, dict) and "path" in r],
        }
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def search_log_summary(days: int = 30) -> dict:
    """Aggregate stats over the last `days` of vault_search calls.
    Output shape:
      {n_calls, n_zero_hit, avg_hits, top_queries, top_paths, since}.
    Used by the /api/vault_search/stats endpoint."""
    p = _log_search_path()
    if not p.exists():
        return {"n_calls": 0, "n_zero_hit": 0, "avg_hits": 0.0,
                "top_queries": [], "top_paths": [], "since": None}
    since = _dt.datetime.utcnow() - _dt.timedelta(days=days)
    n_calls = 0
    n_zero = 0
    hits_sum = 0
    q_counts: dict[str, int] = {}
    path_counts: dict[str, int] = {}
    try:
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts", "")
            try:
                rec_dt = _dt.datetime.fromisoformat(ts.replace("Z", ""))
            except Exception:
                continue
            if rec_dt < since:
                continue
            n_calls += 1
            n_hits = int(r.get("n_hits", 0) or 0)
            hits_sum += n_hits
            if n_hits == 0:
                n_zero += 1
            q = (r.get("query") or "").strip().lower()
            if q:
                q_counts[q] = q_counts.get(q, 0) + 1
            for path in r.get("top_paths") or []:
                if path:
                    path_counts[path] = path_counts.get(path, 0) + 1
    except Exception:
        return {"error": "log_read_failed", "n_calls": 0}
    top_q = sorted(q_counts.items(), key=lambda kv: -kv[1])[:10]
    top_p = sorted(path_counts.items(), key=lambda kv: -kv[1])[:10]
    return {
        "n_calls": n_calls,
        "n_zero_hit": n_zero,
        "zero_hit_pct": round(100 * n_zero / max(n_calls, 1), 1),
        "avg_hits": round(hits_sum / max(n_calls, 1), 2),
        "top_queries": [{"query": q, "n": n} for q, n in top_q],
        "top_paths":   [{"path":  p, "n": n} for p, n in top_p],
        "since": since.isoformat() + "Z",
    }
