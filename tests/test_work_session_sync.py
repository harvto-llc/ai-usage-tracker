import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src import usage_ledger, work_ledger, work_session_sync


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))


def make_repo(path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True)


def claude_event(cwd, *, request_id="request-1", tokens=100):
    return {
        "timestamp": datetime(2026, 7, 25, 12, tzinfo=timezone.utc).isoformat(),
        "sessionId": "claude-session",
        "cwd": str(cwd),
        "gitBranch": "feature/session-attribution",
        "message": {
            "id": request_id,
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {"input_tokens": tokens, "output_tokens": 20},
        },
    }


def test_enabled_collection_rolls_affected_session_into_work_ledger(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    session_file.write_text(json.dumps(claude_event(repo)) + "\n")
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = usage_ledger.sync_provider("claude")

    assert result["affected_sessions"] == ["claude-session"]
    sessions = work_ledger.list_ai_sessions(provider="claude")
    assert len(sessions) == 1
    assert sessions[0]["cwd"] == str(repo)
    assert sessions[0]["models"] == ["claude-opus-5"]
    assert sessions[0]["input_tokens"] == 100
    assert sessions[0]["output_tokens"] == 20
    assert sessions[0]["repository_id"] is not None
    assert sessions[0]["branch"] == "feature/session-attribution"
    assert work_ledger.list_repositories()[0]["enabled"] is False


def test_enabling_collection_can_backfill_existing_normalized_events(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text(json.dumps(claude_event(repo)) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
    assert work_ledger.list_ai_sessions() == []

    work_ledger.configure_collection(enabled=True, now=2)
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = work_session_sync.sync_normalized_sessions("claude")

    assert result == {"provider": "claude", "status": "ok", "sessions": 1}
    assert len(work_ledger.list_ai_sessions()) == 1


def test_claude_branch_context_is_found_in_bounded_file_tail(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    rows = [json.dumps(claude_event(repo, request_id="request-tail"))]
    rows.extend(json.dumps({"type": "progress"}) for _ in range(5_000))
    rows.append(json.dumps({
        "timestamp": "2026-07-25T12:02:00Z",
        "type": "attachment",
        "sessionId": "claude-session",
        "cwd": str(repo),
        "gitBranch": "feature/latest-tail-branch",
        "customTitle": "Latest attribution title",
    }))
    session_file.write_text("\n".join(rows) + "\n")
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")

    session = work_ledger.list_ai_sessions(provider="claude")[0]
    assert session["branch"] == "feature/latest-tail-branch"
    assert session["native_title"] == "Latest attribution title"
    assert session["source_modified_at_us"] is not None


def test_claude_custom_title_backfills_from_middle_then_refreshes(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    rows = [json.dumps(claude_event(repo, request_id="request-middle"))]
    rows.extend(json.dumps({"type": "progress"}) for _ in range(5_000))
    rows.append(json.dumps({
        "type": "custom-title",
        "sessionId": "claude-session",
        "customTitle": "Historical provider rename",
    }))
    rows.extend(json.dumps({"type": "progress"}) for _ in range(5_000))
    session_file.write_text("\n".join(rows) + "\n")
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")

    assert work_ledger.list_ai_sessions(provider="claude")[0]["native_title"] == (
        "Historical provider rename"
    )

    with session_file.open("a") as handle:
        handle.write(json.dumps({
            "type": "custom-title",
            "sessionId": "claude-session",
            "customTitle": "Latest provider rename",
        }) + "\n")
    with patch("pathlib.Path.home", return_value=tmp_path):
        work_session_sync.sync_normalized_sessions("claude")

    assert work_ledger.list_ai_sessions(provider="claude")[0]["native_title"] == (
        "Latest provider rename"
    )


def test_codex_session_meta_cwd_survives_incremental_append(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "25"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    rows = [
        {
            "timestamp": "2026-07-25T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "codex-session",
                "cwd": str(repo),
                "git": {
                    "branch": "codex/smart-report",
                    "commit_hash": "a" * 40,
                },
            },
        },
        {
            "timestamp": "2026-07-25T12:01:00Z",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("codex")
        with session_file.open("a") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-07-25T12:02:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 200, "output_tokens": 40}},
                },
            }) + "\n")
        usage_ledger.sync_provider("codex")

    session = work_ledger.list_ai_sessions(provider="codex")[0]
    assert session["cwd"] == str(repo)
    assert session["branch"] == "codex/smart-report"
    assert session["head_sha"] == "a" * 40
    assert session["input_tokens"] == 300
    assert session["output_tokens"] == 60


def test_codex_thread_name_refreshes_without_new_usage(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "25"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    session_file.write_text(json.dumps({
        "timestamp": "2026-07-25T12:00:00Z",
        "type": "session_meta",
        "payload": {"id": "codex-renamed", "cwd": str(repo)},
    }) + "\n" + json.dumps({
        "timestamp": "2026-07-25T12:01:00Z",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 100}},
        },
    }) + "\n")
    state_path = tmp_path / ".codex" / "state_5.sqlite"
    with sqlite3.connect(state_path) as conn:
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, name TEXT)"
        )
        conn.execute(
            "INSERT INTO threads(id, title, name) VALUES (?, ?, ?)",
            ("codex-renamed", "Generated title", "First provider name"),
        )
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("codex")

    assert work_ledger.list_ai_sessions(provider="codex")[0]["native_title"] == (
        "First provider name"
    )

    with sqlite3.connect(state_path) as conn:
        conn.execute(
            "UPDATE threads SET name = ? WHERE id = ?",
            ("Renamed provider session", "codex-renamed"),
        )
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = work_session_sync.sync_normalized_sessions("codex")

    assert result["sessions"] == 1
    assert work_ledger.list_ai_sessions(provider="codex")[0]["native_title"] == (
        "Renamed provider session"
    )


def test_missing_bounded_title_does_not_erase_captured_provider_name(tmp_path):
    work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "claude-renamed",
        "native_title": "Captured rename",
    })

    work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "claude-renamed",
        "native_title": None,
    })

    assert work_ledger.list_ai_sessions(provider="claude")[0]["native_title"] == (
        "Captured rename"
    )
