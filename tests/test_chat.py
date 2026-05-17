"""Chat orchestrator tests — stubbed Anthropic client so they run without a key.

Covers the 5 paths that can break:
  1. single-shot answer (no tools)
  2. one tool call → final answer
  3. multi-tool chain
  4. tool exception → graceful recovery
  5. history threading
Plus a smoke check that every registered tool executes end-to-end.
"""
from __future__ import annotations
import json
import os
import pytest
from types import SimpleNamespace
from unittest.mock import patch

# Force-set (setdefault is a no-op if parent shell has it as empty string,
# which my shell does because uvicorn unsets it for me; without this every
# test would short-circuit on the "set ANTHROPIC_API_KEY" guard).
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-stub"

from reip import chat


@pytest.fixture(autouse=True)
def _ensure_api_key():
    """Every test in this file needs a non-empty ANTHROPIC_API_KEY so the
    orchestrator's env guard passes. The no-key test deletes it explicitly
    via monkeypatch.delenv."""
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-stub"
    yield
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-stub"


def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def _resp(stop_reason, content):
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class _Stub:
    """Anthropic client stub. Pass `script` = list of responses."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, **kw):
        # Accept any kwargs (api_key, auth_token, default_headers, …)
        return self

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return self._script.pop(0)


def test_single_shot_no_tools():
    stub = _Stub([_resp("end_turn", [_block("text", text="ok")])])
    with patch("anthropic.Anthropic", stub):
        out = chat.chat("hi")
    assert out["reply"] == "ok"
    assert out["tool_calls"] == []
    assert len(stub.calls) == 1


def test_tool_use_loop_threads_result():
    stub = _Stub([
        _resp("tool_use", [
            _block("tool_use", id="t1", name="top_msas", input={"limit": 2}),
        ]),
        _resp("end_turn", [_block("text", text="here you go")]),
    ])
    with patch("anthropic.Anthropic", stub):
        out = chat.chat("top 2 msas")
    assert out["reply"] == "here you go"
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["name"] == "top_msas"
    assert out["tool_calls"][0]["ok"] is True
    # Second call had tool_result threaded
    msgs = stub.calls[1]["messages"]
    last_user = msgs[-1]
    assert last_user["role"] == "user"
    tool_block = next(c for c in last_user["content"] if c.get("type") == "tool_result")
    assert tool_block["tool_use_id"] == "t1"
    parsed = json.loads(tool_block["content"])
    assert isinstance(parsed, list)


def test_multi_tool_chain():
    stub = _Stub([
        _resp("tool_use", [_block("tool_use", id="a", name="msa_detail", input={"cbsa_code": "32820"})]),
        _resp("tool_use", [_block("tool_use", id="b", name="parse_remarks", input={"text": "motivated"})]),
        _resp("end_turn", [_block("text", text="done")]),
    ])
    with patch("anthropic.Anthropic", stub):
        out = chat.chat("look this up")
    assert [t["name"] for t in out["tool_calls"]] == ["msa_detail", "parse_remarks"]


def test_tool_error_returns_gracefully():
    # Force the executor to surface an "error" dict on a missing cbsa
    stub = _Stub([
        _resp("tool_use", [_block("tool_use", id="x", name="msa_detail", input={"cbsa_code": "99999"})]),
        _resp("end_turn", [_block("text", text="not found")]),
    ])
    with patch("anthropic.Anthropic", stub):
        out = chat.chat("what is 99999")
    # Tool ran; result has 'error' key; agent still got a final answer
    assert out["reply"] == "not found"
    tool_result_content = stub.calls[1]["messages"][-1]["content"][0]["content"]
    assert "error" in json.loads(tool_result_content)


def test_history_threading_preserves_order():
    stub = _Stub([_resp("end_turn", [_block("text", text="ok")])])
    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    with patch("anthropic.Anthropic", stub):
        chat.chat("c", history=history)
    roles = [m["role"] for m in stub.calls[0]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_no_credentials_returns_friendly_error(monkeypatch):
    """chat() must surface a friendly error when neither API key nor
    Claude Code OAuth token is reachable. We block: env, .env file,
    and keychain."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(chat, "_get_claude_code_oauth_token", lambda: None)
    out = chat.chat("hi")
    assert "error" in out
    assert "credentials" in out["error"].lower() or "ANTHROPIC_API_KEY" in out["error"]


def test_max_tool_iters_caps_runaway_loop():
    # Infinite tool-use loop — orchestrator must stop at max_tool_iters
    stub = _Stub([
        _resp("tool_use", [_block("tool_use", id=f"t{i}", name="parse_remarks", input={"text": "x"})])
        for i in range(20)
    ])
    with patch("anthropic.Anthropic", stub):
        out = chat.chat("loop", max_tool_iters=3)
    # 3 iterations means at most 3 model calls
    assert len(stub.calls) <= 3
    assert "max tool iterations" in out["reply"]


# ---- Tool executors (no LLM in the loop, pure data path) -----------------

def test_every_tool_executes():
    cases = [
        ("top_zips",      {"limit": 2}),
        ("top_msas",      {"limit": 2}),
        ("msa_detail",    {"cbsa_code": "32820"}),
        ("underwrite",    {"purchase_price": 100000, "monthly_rent": 1000}),
        ("avm_zips",      {"direction": "cold", "limit": 2}),
        ("parse_remarks", {"text": "trustees sale, REO, motivated seller"}),
        ("current_rates", {}),
        ("recent_decisions", {"limit": 5}),
        ("vault_search",  {"query": "framework", "limit": 2}),
        ("brrrr_walkthrough", {
            "purchase_price": 115000, "rehab_cost": 42000, "arv": 215000,
            "monthly_rent": 1750, "annual_opex": 7200, "holding_cost": 4000,
        }),
        # run_score_backtest hits real DB + 1000 bootstrap iters ~3s — slow
        # for unit-test budget. Smoke-tested via tests/test_score_backtest.py
        # and the live one-shot at commit time instead.
    ]
    for name, args in cases:
        out = chat._execute(name, args)
        # Must JSON-serialize cleanly
        json.dumps(out, default=str)


# ---- Decision ledger (fast-weight context) -------------------------------

def test_record_decision_appends_and_recent_reads(tmp_path, monkeypatch):
    """record_decision → recent_decisions round-trip. Uses a temp ledger
    so the test doesn't touch Richard's real ~/.reip/decisions.jsonl."""
    from reip import decision_ledger
    tmp_log = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decision_ledger, "_path", lambda: tmp_log)

    # Empty ledger → empty list, empty context block
    assert decision_ledger.recent() == []
    assert decision_ledger.render_context_block() == ""

    # Record a verdict via the chat tool dispatcher
    out = chat._execute("record_decision", {
        "zip": "38116",
        "verdict": "PASS",
        "reason": "Wolf River 100y flood plain — climate overlay missed it.",
        "state": "TN",
        "price": 85000,
        "verdict_gate": "GREEN",
    })
    assert "error" not in out
    assert out["zip"] == "38116"
    assert out["verdict"] == "PASS"
    assert out["state"] == "TN"
    assert out["price"] == 85000

    # Read back via the chat tool dispatcher
    rows = chat._execute("recent_decisions", {"limit": 5})
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["zip"] == "38116"
    assert rows[0]["verdict"] == "PASS"

    # The context-block renderer surfaces it for the system prompt
    block = decision_ledger.render_context_block(limit=5)
    assert "38116" in block
    assert "PASS" in block
    assert "Wolf River" in block


def test_record_decision_rejects_bad_verdict(tmp_path, monkeypatch):
    from reip import decision_ledger
    monkeypatch.setattr(decision_ledger, "_path", lambda: tmp_path / "d.jsonl")
    out = chat._execute("record_decision", {
        "zip": "12345", "verdict": "MAYBE", "reason": "idk",
    })
    assert "error" in out
    assert "BUY" in out["error"]  # enumerates valid options


def test_record_decision_requires_reason(tmp_path, monkeypatch):
    from reip import decision_ledger
    monkeypatch.setattr(decision_ledger, "_path", lambda: tmp_path / "d.jsonl")
    out = chat._execute("record_decision", {
        "zip": "12345", "verdict": "BUY", "reason": "",
    })
    assert "error" in out


# ---- Vault knowledge (Obsidian wiring) -----------------------------------

def test_knowledge_block_empty_when_folder_missing(tmp_path, monkeypatch):
    """No vault → empty string. Keeps the system-prompt cache key stable
    for users without a vault."""
    monkeypatch.setenv("REIP_VAULT_PATH", str(tmp_path / "no-such-vault"))
    from reip import vault_knowledge
    assert vault_knowledge.load_knowledge_block() == ""


def test_knowledge_block_empty_when_folder_exists_but_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("REIP_VAULT_PATH", str(tmp_path))
    (tmp_path / "Real Estate Platform" / "Knowledge").mkdir(parents=True)
    from reip import vault_knowledge
    assert vault_knowledge.load_knowledge_block() == ""


def test_knowledge_block_loads_md_files_skipping_underscore(tmp_path, monkeypatch):
    monkeypatch.setenv("REIP_VAULT_PATH", str(tmp_path))
    k = tmp_path / "Real Estate Platform" / "Knowledge"
    k.mkdir(parents=True)
    (k / "principles.md").write_text(
        "---\ntype: knowledge\n---\n# Principles\nCap rate is the wrong metric.\n"
    )
    (k / "_archived.md").write_text("# Archived\nshould not appear")
    (k / "msas.md").write_text("# MSAs\nLaunch: Memphis, Indy, Columbus.\n")

    from reip import vault_knowledge
    block = vault_knowledge.load_knowledge_block()
    assert "Cap rate is the wrong metric" in block
    assert "Memphis, Indy, Columbus" in block
    assert "should not appear" not in block
    # YAML frontmatter stripped
    assert "type: knowledge" not in block
    # Filename as header (file stem)
    assert "### principles" in block
    assert "### msas" in block


def test_vault_search_logs_each_call(tmp_path, monkeypatch):
    """Every search call appends to vault_search_log.jsonl. The log
    is the EvolveMem-style measure-first scaffolding from MST-062 — we
    capture call volume + hit-set BEFORE deciding whether ranking
    weights are worth tuning."""
    from reip import vault_knowledge
    log_path = tmp_path / "vault_search_log.jsonl"
    monkeypatch.setattr(vault_knowledge, "_log_search_path", lambda: log_path)
    monkeypatch.setenv("REIP_VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault" / "Real Estate Platform").mkdir(parents=True)
    (tmp_path / "vault" / "Real Estate Platform" / "memphis.md").write_text("Memphis cashflow buy box")
    (tmp_path / "vault" / "Real Estate Platform" / "pittsburgh.md").write_text("Pittsburgh resilience")

    chat._execute("vault_search", {"query": "memphis cashflow"})
    chat._execute("vault_search", {"query": "no-such-content-anywhere"})
    chat._execute("vault_search", {"query": "pittsburgh"})

    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    queries = [r["query"] for r in lines]
    assert queries == ["memphis cashflow", "no-such-content-anywhere", "pittsburgh"]
    # Hit counts: memphis matches 1 file (multi-token), no-such matches 0, pittsburgh matches 1
    assert lines[0]["n_hits"] >= 1
    assert lines[1]["n_hits"] == 0
    assert lines[2]["n_hits"] >= 1


def test_vault_search_log_summary(tmp_path, monkeypatch):
    """search_log_summary() aggregates the right shape for the API
    endpoint — call count, zero-hit %, top queries, top paths."""
    from reip import vault_knowledge
    log_path = tmp_path / "vault_search_log.jsonl"
    monkeypatch.setattr(vault_knowledge, "_log_search_path", lambda: log_path)
    # Hand-write a few log entries spanning 3 distinct queries + paths
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat() + "Z"
    rows = [
        {"ts": now, "query": "memphis",    "tokens": ["memphis"],    "n_hits": 2, "top_paths": ["A.md", "B.md"]},
        {"ts": now, "query": "memphis",    "tokens": ["memphis"],    "n_hits": 2, "top_paths": ["A.md", "B.md"]},
        {"ts": now, "query": "pricing",    "tokens": ["pricing"],    "n_hits": 1, "top_paths": ["C.md"]},
        {"ts": now, "query": "broken q",   "tokens": ["broken", "q"],"n_hits": 0, "top_paths": []},
    ]
    log_path.write_text("\n".join(json.dumps(r) for r in rows))

    out = vault_knowledge.search_log_summary(days=30)
    assert out["n_calls"] == 4
    assert out["n_zero_hit"] == 1
    assert out["zero_hit_pct"] == 25.0
    assert out["avg_hits"] == 1.25
    # Top query: "memphis" with 2 calls
    assert out["top_queries"][0] == {"query": "memphis", "n": 2}
    # Top path: A.md (2 returns) ties B.md (2 returns) — first should be one of them
    assert out["top_paths"][0]["n"] == 2


def test_vault_search_log_summary_empty(tmp_path, monkeypatch):
    """Missing log file → safe zeros, not a crash."""
    from reip import vault_knowledge
    monkeypatch.setattr(vault_knowledge, "_log_search_path", lambda: tmp_path / "nonexistent.jsonl")
    out = vault_knowledge.search_log_summary(days=30)
    assert out["n_calls"] == 0
    assert out["n_zero_hit"] == 0


def test_vault_search_finds_matches_and_clamps_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("REIP_VAULT_PATH", str(tmp_path))
    (tmp_path / "Real Estate Platform").mkdir(parents=True)
    (tmp_path / "Real Estate Platform" / "alpha.md").write_text(
        "Photo-assisted rehab heuristic is alpha source #8."
    )
    (tmp_path / "Real Estate Platform" / "beta.md").write_text(
        "Cap rate is the wrong quality metric.\nUse realized accuracy."
    )
    (tmp_path / "Daily" / "2026-05-15.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Daily" / "2026-05-15.md").write_text("Built reip vault wiring today.")

    out = chat._execute("vault_search", {"query": "alpha", "limit": 5})
    assert isinstance(out, list)
    assert len(out) >= 1
    assert any("alpha.md" in h["path"] for h in out)
    # Excerpt should contain the match (case-insensitive)
    assert any("alpha source" in h["excerpt"].lower() for h in out)

    # Empty query → error shape
    err = chat._execute("vault_search", {"query": ""})
    assert isinstance(err, list) and "error" in err[0]

    # Out-of-range limit → clamped, no crash
    out = chat._execute("vault_search", {"query": "the", "limit": 999})
    assert len(out) <= 20  # _SEARCH_MAX_LIMIT


def test_recent_decisions_clamps_limit(tmp_path, monkeypatch):
    """limit must be coerced to 1-50 so a bad LLM arg can't blow context."""
    from reip import decision_ledger
    log = tmp_path / "d.jsonl"
    monkeypatch.setattr(decision_ledger, "_path", lambda: log)
    # Write 60 records
    for i in range(60):
        decision_ledger.append(zip_code=f"{10000+i:05d}", verdict="WATCH",
                               reason=f"test record {i}")
    # Tool requests 999 → must be clamped to 50
    rows = chat._execute("recent_decisions", {"limit": 999})
    assert len(rows) <= 50
    # Tool requests garbage → falls back to default 10
    rows = chat._execute("recent_decisions", {"limit": "bogus"})
    assert len(rows) == 10
