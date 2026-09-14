import os

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

from src import usage_ledger, work_ledger
from src.api import API_SECRET, app


AUTH = {"Authorization": f"Bearer {API_SECRET}"}


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))


def _usage_event(provider, session_id, *, tokens, cwd=None):
    usage_ledger.init()
    with usage_ledger._connect() as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day,
                   session_id, cwd, model, is_message, is_user, input_tokens,
                   output_tokens, cache_read_tokens, cache_write_5m_tokens,
                   cache_write_1h_tokens, reasoning_tokens, request_id
               ) VALUES (?, ?, 0, ?, 1000000, '2026-07-25', ?, ?, ?, 1, 1,
                         ?, 10, 5, 0, 0, 2, NULL)""",
            (
                provider,
                session_id,
                f"/{session_id}.jsonl",
                session_id,
                cwd,
                "model",
                tokens,
            ),
        )
        conn.commit()


def test_reconciliation_keeps_unassigned_explicit_and_reports_coverage_gap():
    _usage_event("claude", "assigned", tokens=100, cwd="/repo")
    _usage_event("claude", "missing", tokens=50)
    work_ledger.upsert_repository({
        "id": "repo-1",
        "display_name": "Repo One",
        "identity_kind": "root_commit",
        "common_dir": "/repo/.git",
        "enabled": True,
    })
    work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "assigned",
        "repository_id": "repo-1",
        "messages": 1,
        "user_messages": 1,
        "requests": 1,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_tokens": 5,
        "reasoning_tokens": 2,
    })

    claude = work_ledger.reconciliation()["providers"]["claude"]

    assert claude["reconciled"] is False
    assert claude["source_totals"]["input_tokens"] == 150
    assert claude["indexed_session_totals"]["input_tokens"] == 100
    assert claude["coverage_gap"]["input_tokens"] == 50
    assert [bucket["display_name"] for bucket in claude["buckets"]] == [
        "Repo One",
        "Unassigned",
    ]


def test_reconciliation_balances_repository_and_unassigned_sessions():
    _usage_event("codex", "assigned", tokens=100)
    _usage_event("codex", "unassigned", tokens=50)
    work_ledger.upsert_repository({
        "id": "repo-1",
        "display_name": "Repo One",
        "identity_kind": "root_commit",
        "common_dir": "/repo/.git",
        "enabled": True,
    })
    for session_id, repository_id, input_tokens in (
        ("assigned", "repo-1", 100),
        ("unassigned", None, 50),
    ):
        work_ledger.upsert_ai_session({
            "provider": "codex",
            "provider_session_id": session_id,
            "repository_id": repository_id,
            "messages": 1,
            "user_messages": 1,
            "requests": 1,
            "input_tokens": input_tokens,
            "output_tokens": 10,
            "cache_tokens": 5,
            "reasoning_tokens": 2,
        })

    codex = work_ledger.reconciliation()["providers"]["codex"]

    assert codex["reconciled"] is True
    assert sum(bucket["totals"]["input_tokens"] for bucket in codex["buckets"]) == 150
    unassigned = next(
        bucket for bucket in codex["buckets"] if bucket["bucket"] == "unassigned"
    )
    assert unassigned["session_count"] == 1


def test_work_ledger_endpoints_are_authenticated_read_only_views():
    client = TestClient(app)

    assert client.get("/work-ledger/status").status_code == 401
    status = client.get("/work-ledger/status", headers=AUTH)
    reconciliation = client.get("/work-ledger/reconciliation", headers=AUTH)
    repositories = client.get("/work-ledger/repositories", headers=AUTH)
    sessions = client.get("/work-ledger/sessions", headers=AUTH)
    events = client.get("/work-ledger/events", headers=AUTH)

    assert status.status_code == 200
    assert status.json()["settings"]["collection_enabled"] is False
    assert reconciliation.status_code == 200
    assert set(reconciliation.json()["providers"]) == {"claude", "codex"}
    assert repositories.json() == {"repositories": []}
    assert sessions.json() == {"sessions": []}
    assert events.json() == {"events": []}
