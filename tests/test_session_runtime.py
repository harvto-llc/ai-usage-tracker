import hashlib
import json

import pytest

from src import session_runtime, usage_ledger, work_ledger


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))


def test_live_claude_pid_and_recent_codex_file_produce_runtime_states(tmp_path, monkeypatch):
    claude_source = tmp_path / "claude.jsonl"
    codex_source = tmp_path / "codex.jsonl"
    claude_source.write_text("{}\n")
    codex_source.write_text("{}\n")
    now = 10_000.0
    work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "claude-live",
        "source_path": str(claude_source),
        "source_modified_at_us": round((now - 600) * 1_000_000),
        "native_title": "Review billing",
    })
    work_ledger.upsert_ai_session({
        "provider": "codex",
        "provider_session_id": "codex-active",
        "source_path": str(codex_source),
        "source_modified_at_us": round((now - 30) * 1_000_000),
        "branch": "codex/session-tags",
    })
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "123.json").write_text(json.dumps({
        "pid": 123,
        "sessionId": "claude-live",
    }))
    monkeypatch.setattr(session_runtime, "_process_is_running", lambda pid: pid == 123)

    sessions = session_runtime.list_session_views(active_only=True, now=now, home=tmp_path)
    by_provider = {row["provider"]: row for row in sessions}

    assert by_provider["claude"]["runtime_state"] == "running"
    assert by_provider["claude"]["display_name"] == "Review billing"
    assert by_provider["codex"]["runtime_state"] == "active"
    assert by_provider["codex"]["display_name"] == "codex/session-tags"


def test_live_claude_metadata_creates_session_without_erasing_usage(tmp_path, monkeypatch):
    work_ledger.configure_collection(enabled=True, now=1)
    work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "claude-live",
        "input_tokens": 100,
        "models": ["claude-opus-5"],
    }, now=1)
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "123.json").write_text(json.dumps({
        "pid": 123,
        "sessionId": "claude-live",
        "cwd": str(tmp_path / "repo"),
        "startedAt": 1_000,
        "updatedAt": 2_000,
        "name": "live-review",
        "status": "busy",
    }))
    monkeypatch.setattr(session_runtime, "_process_is_running", lambda pid: pid == 123)

    result = session_runtime.sync_live_claude_sessions(home=tmp_path, now=3)
    session = work_ledger.list_ai_sessions()[0]

    assert result["sessions"] == 1
    assert session["native_title"] == "live-review"
    assert session["input_tokens"] == 100
    assert session["models"] == ["claude-opus-5"]


def test_agent_tts_nickname_import_is_non_destructive(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text("{}\n")
    session_id = work_ledger.upsert_ai_session({
        "provider": "codex",
        "provider_session_id": "codex-session",
        "source_path": str(source),
        "source_modified_at_us": 1_000_000,
    })
    nickname_dir = tmp_path / ".local" / "share" / "ai-agent-tts"
    nickname_dir.mkdir(parents=True)
    path_hash = hashlib.md5(str(source).encode()).hexdigest()
    (nickname_dir / "session-nicknames.json").write_text(json.dumps({path_hash: "TTS name"}))

    first = session_runtime.list_session_views(now=2, home=tmp_path)[0]
    assert first["nickname"] == "TTS name"
    assert first["display_name"] == "TTS name"
    assert first["nickname_source"] == "agent_tts_import"

    work_ledger.set_session_nickname(session_id, "Usage name")
    second = session_runtime.list_session_views(now=2, home=tmp_path)[0]
    assert second["nickname"] == "Usage name"


def test_provider_session_name_precedes_local_nickname(tmp_path):
    session_id = work_ledger.upsert_ai_session({
        "provider": "codex",
        "provider_session_id": "codex-renamed",
        "native_title": "Provider rename",
    })
    work_ledger.set_session_nickname(session_id, "Local alias")

    session = session_runtime.list_session_views(now=2, home=tmp_path)[0]

    assert session["display_name"] == "Provider rename"
    assert session["nickname"] == "Local alias"


def test_session_assignment_and_global_default_are_presented_separately(tmp_path):
    project = work_ledger.create_work_item(kind="project", name="Usage Tracker", now=1)
    session_id = work_ledger.upsert_ai_session({
        "provider": "claude",
        "provider_session_id": "claude-session",
        "source_modified_at_us": 10_000_000,
    })
    work_ledger.switch_active_work_item(project["id"], at_us=1, now=1)

    automatic = session_runtime.list_session_views(now=10, home=tmp_path)[0]
    assert automatic["assignment_mode"] == "automatic"
    assert automatic["effective_work_label"] == "Usage Tracker"
    assert automatic["attribution_source"] == "default"

    work_ledger.set_session_work_assignment(session_id, mode="unassigned", now=2)
    unassigned = session_runtime.list_session_views(now=10, home=tmp_path)[0]
    assert unassigned["assignment_mode"] == "unassigned"
    assert unassigned["effective_work_label"] == "Unassigned"
    assert unassigned["attribution_source"] == "session"
