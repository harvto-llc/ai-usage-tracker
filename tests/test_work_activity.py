import json
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from src import usage_ledger, work_activity, work_ledger


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    return db_path


def _make_repo(path):
    path.mkdir()
    subprocess.run(
        ["git", "-C", str(path), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )


def _claude_tool_event(
    repo,
    tool_name,
    tool_input,
    *,
    minute,
    request_id,
    tool_id=None,
):
    tool_block = {"type": "tool_use", "name": tool_name, "input": tool_input}
    if tool_id:
        tool_block["id"] = tool_id
    return {
        "timestamp": f"2026-07-25T12:{minute:02d}:00Z",
        "sessionId": "claude-session",
        "cwd": str(repo),
        "message": {
            "id": request_id,
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [tool_block],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    }


def test_extract_activity_classifies_without_returning_arguments():
    claude = _claude_tool_event(
        "/repo",
        "Bash",
        {"command": "python -m pytest tests -q", "description": "secret"},
        minute=0,
        request_id="one",
    )
    codex_build = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "npm run build", "workdir": "/secret"}),
        },
    }
    codex_edit = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "input": "private patch body",
        },
    }

    assert work_activity.extract_activity("claude", claude) == [{
        "kind": "test",
        "metadata": {"tool_name": "Bash", "command_category": "test"},
    }]
    assert work_activity.extract_activity("codex", codex_build) == [{
        "kind": "build",
        "metadata": {"tool_name": "exec_command", "command_category": "build"},
    }]
    assert work_activity.extract_activity("codex", codex_edit) == [{
        "kind": "edit",
        "metadata": {"tool_name": "apply_patch", "command_category": "edit"},
    }]
    assert "secret" not in json.dumps(work_activity.extract_activity("claude", claude))
    assert "private patch body" not in json.dumps(
        work_activity.extract_activity("codex", codex_edit)
    )


def test_disabled_collection_does_not_create_provider_activity_cursor(tmp_path, activity_db):
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text("{}\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = work_activity.sync_provider_activity("claude")

    assert result == {"provider": "claude", "status": "paused", "events_inserted": 0}
    with sqlite3.connect(activity_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_activity_cursors").fetchone()[0] == 0


def test_usage_sync_indexes_tool_activity_incrementally_and_idempotently(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    session_file.write_text(
        json.dumps(_claude_tool_event(
            repo,
            "Edit",
            {"file_path": "/secret/file.py", "old_string": "private"},
            minute=0,
            request_id="one",
        )) + "\n"
    )
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        first = work_activity.sync_provider_activity("claude", sync_sessions=False)
        second = work_activity.sync_provider_activity("claude", sync_sessions=False)
        with session_file.open("a") as handle:
            handle.write(json.dumps(_claude_tool_event(
                repo,
                "Bash",
                {"command": "python -m pytest tests -q"},
                minute=1,
                request_id="two",
            )) + "\n")
        usage_ledger.sync_provider("claude")
        third = work_activity.sync_provider_activity("claude", sync_sessions=False)

    assert first["events_inserted"] == 1
    assert second["events_inserted"] == 0
    assert third["events_inserted"] == 1
    events = work_ledger.list_activity_events(provider="claude")
    assert {event["kind"] for event in events} == {"edit", "test"}
    assert all(event["ai_session_id"] for event in events)
    assert all(event["repository_id"] for event in events)
    serialized = json.dumps(events)
    assert "/secret/file.py" not in serialized
    assert "python -m pytest" not in serialized


def test_codex_structural_outputs_are_not_counted_as_tool_calls():
    output = {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "secret command output",
        },
    }

    assert work_activity.extract_activity("codex", output) == []


def test_provider_call_identity_prevents_duplicates_after_file_rewrite(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    first_event = _claude_tool_event(
        repo,
        "Bash",
        {"command": "git status"},
        minute=0,
        request_id="one",
        tool_id="tool-call-1",
    )
    session_file.write_text(json.dumps(first_event) + "\n")
    work_ledger.configure_collection(enabled=True, now=1)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        assert work_activity.sync_provider_activity("claude", sync_sessions=False)[
            "events_inserted"
        ] == 1
        first_event["message"]["content"][0]["input"]["command"] = (
            "git status --short --branch"
        )
        session_file.write_text(json.dumps(first_event) + "\n")
        assert work_activity.sync_provider_activity("claude", sync_sessions=False)[
            "events_inserted"
        ] == 0

    events = work_ledger.list_activity_events(provider="claude")
    assert len(events) == 1
    assert events[0]["kind"] == "git"


def test_warm_sync_does_not_reparse_consumed_history(tmp_path):
    root = tmp_path / ".claude" / "projects" / "repo"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    rows = [
        _claude_tool_event(
            "/repo",
            "Read",
            {"file_path": f"/private/{index}.py"},
            minute=index % 60,
            request_id=f"request-{index}",
            tool_id=f"tool-{index}",
        )
        for index in range(100)
    ]
    session_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    work_ledger.configure_collection(enabled=True, now=1)
    original_extract = work_activity.extract_activity

    with patch("pathlib.Path.home", return_value=tmp_path), patch.object(
        work_activity,
        "extract_activity",
        wraps=original_extract,
    ) as extract:
        cold = work_activity.sync_provider_activity("claude", sync_sessions=False)
        cold_parse_count = extract.call_count
        warm = work_activity.sync_provider_activity("claude", sync_sessions=False)

    assert cold["events_inserted"] == 100
    assert cold_parse_count == 100
    assert warm["events_inserted"] == 0
    assert extract.call_count == cold_parse_count
