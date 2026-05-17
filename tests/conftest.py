"""Pytest fixtures applied across every test in the suite."""
from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def _quarantine_user_logs(tmp_path_factory, monkeypatch):
    """Redirect log-file writers in `reip/*` away from the real
    `~/.reip/` directory during tests.

    Without this, every test run that exercises vault_search /
    record_decision pollutes the user's real logs with synthetic
    data — which then bleeds into /api/vault_search/stats and
    /api/chat/usage outputs. Pinning the paths to a tmp dir keeps
    the suite hermetic.
    """
    base = tmp_path_factory.mktemp("reip_user_logs")
    # vault_search log
    from reip import vault_knowledge
    monkeypatch.setattr(vault_knowledge, "_log_search_path",
                        lambda b=base: b / "vault_search_log.jsonl")
    # Individual tests that need to point the decision-ledger or
    # vault path at their OWN tmp dir still can — this just defaults.
    yield
