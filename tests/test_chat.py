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

    def __call__(self, api_key=None):
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


def test_no_api_key_returns_friendly_error(monkeypatch):
    """chat() must surface a friendly error when no key is reachable.
    We block both the env var AND the .env reload path so a real key
    sitting on disk doesn't accidentally satisfy this test."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Stub load_dotenv to a no-op so the on-disk .env can't sneak in
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    out = chat.chat("hi")
    assert "error" in out
    assert "ANTHROPIC_API_KEY" in out["error"] or "Set ANTHROPIC_API_KEY" in out["error"]


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
        ("parse_remarks", {"text": "motivated seller"}),
    ]
    for name, args in cases:
        out = chat._execute(name, args)
        # Must JSON-serialize cleanly
        json.dumps(out, default=str)
