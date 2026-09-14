import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src import usage_ledger


@pytest.fixture(autouse=True)
def _activity_db(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(tmp_path / "activity.db"))


def _claude_event(at: datetime, request_id: str, input_tokens: int = 100, cwd: str | None = None) -> dict:
    event = {
        "timestamp": at.isoformat(),
        "sessionId": "claude-session",
        "message": {
            "id": request_id,
            "role": "assistant",
            "model": "claude-fable-5",
            "usage": {"input_tokens": input_tokens, "output_tokens": 50},
        },
    }
    if cwd is not None:
        event["cwd"] = cwd
    return event


def _write_rows(path, rows, *, trailing_newline=True):
    text = "\n".join(json.dumps(row) for row in rows)
    if trailing_newline:
        text += "\n"
    path.write_text(text)


def test_claude_sync_consumes_only_appended_complete_lines(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    _write_rows(session_file, [_claude_event(now - timedelta(minutes=2), "one")])

    with patch("pathlib.Path.home", return_value=tmp_path):
        first = usage_ledger.sync_provider("claude")
        with session_file.open("a") as handle:
            handle.write(json.dumps(_claude_event(now - timedelta(minutes=1), "two", 200)) + "\n")
        with patch.object(
            usage_ledger,
            "_parse_claude_entry",
            wraps=usage_ledger._parse_claude_entry,
        ) as parser:
            second = usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(hours=1), now
        )

    assert first["events_inserted"] == 1
    assert second["events_inserted"] == 1
    assert parser.call_count == 1
    assert sum(row["input_tokens"] + row["output_tokens"] for row in events) == 400


def test_partial_line_is_not_consumed_until_completed(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    session_file = root / "partial.jsonl"
    _write_rows(session_file, [_claude_event(now, "partial")], trailing_newline=False)

    with patch("pathlib.Path.home", return_value=tmp_path):
        first = usage_ledger.sync_provider("claude")
        with session_file.open("a") as handle:
            handle.write("\n")
        second = usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert first["events_inserted"] == 0
    assert second["events_inserted"] == 1
    assert len(events) == 1


def test_claude_request_deduplication_persists_across_appends(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    session_file = root / "duplicates.jsonl"
    duplicate = _claude_event(now - timedelta(minutes=1), "same")
    _write_rows(session_file, [duplicate, duplicate])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        with session_file.open("a") as handle:
            handle.write(json.dumps(duplicate) + "\n")
        usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(hours=1), now
        )

    assert len(events) == 3
    assert sum(row["input_tokens"] + row["output_tokens"] for row in events) == 150


def test_claude_forked_files_dedupe_usage_and_activity_globally(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    assistant = _claude_event(now, "shared-response")
    user = {
        "timestamp": (now - timedelta(seconds=1)).isoformat(),
        "sessionId": "claude-session",
        "uuid": "shared-user-event",
        "message": {"role": "user", "content": "question"},
    }
    for project in ("original", "fork"):
        root = tmp_path / ".claude" / "projects" / project
        root.mkdir(parents=True)
        _write_rows(root / "session.jsonl", [user, assistant])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert len(events) == 4
    assert sum(event["input_tokens"] + event["output_tokens"] for event in events) == 150
    assert sum(event["is_message"] for event in events) == 2
    assert sum(event["is_user"] for event in events) == 1
    assert {event["surface"] for event in events} == {"claude-code"}


def test_claude_desktop_sessions_receive_desktop_surface(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = (
        tmp_path / "Library" / "Application Support" / "Claude"
        / "local-agent-mode-sessions" / "outer" / "local" / ".claude"
        / "projects" / "project"
    )
    root.mkdir(parents=True)
    _write_rows(root / "desktop.jsonl", [_claude_event(now, "desktop-response")])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert len(events) == 1
    assert events[0]["surface"] == "desktop"


def test_same_path_rewrite_starts_a_new_file_generation(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    session_file = root / "rewritten.jsonl"
    _write_rows(session_file, [_claude_event(now - timedelta(minutes=2), "old")])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        replacement = _claude_event(now - timedelta(minutes=1), "new", 500)
        replacement["padding"] = "x" * 500
        _write_rows(session_file, [replacement])
        result = usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(hours=1), now
        )

    assert result["events_inserted"] == 1
    assert len(events) == 2
    assert {row["input_tokens"] for row in events} == {100, 500}


def test_renamed_file_keeps_cursor_without_duplicate_events(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    original = root / "original.jsonl"
    renamed = root / "renamed.jsonl"
    _write_rows(original, [_claude_event(now, "one")])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        original.rename(renamed)
        result = usage_ledger.sync_provider("claude")
        events = usage_ledger.events_between(
            "claude", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert result["events_inserted"] == 0
    assert len(events) == 1


def test_codex_append_preserves_model_cursor_state(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "24"
    root.mkdir(parents=True)
    session_file = root / "session.jsonl"
    rows = [
        {
            "timestamp": (now - timedelta(minutes=3)).isoformat(),
            "type": "session_meta",
            "payload": {"id": "codex-session", "originator": "loop", "source": "vscode"},
        },
        {
            "timestamp": (now - timedelta(minutes=2)).isoformat(),
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra"},
        },
        {
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        },
    ]
    _write_rows(session_file, rows)

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("codex")
        with session_file.open("a") as handle:
            handle.write(json.dumps({
                "timestamp": now.isoformat(),
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 200, "output_tokens": 40}},
                },
            }) + "\n")
        result = usage_ledger.sync_provider("codex")
        events = usage_ledger.events_between(
            "codex", now - timedelta(hours=1), now
        )

    assert result["events_inserted"] == 1
    assert [row["model"] for row in events] == ["gpt-5.6-terra", "gpt-5.6-terra"]
    assert [row["session_id"] for row in events] == ["codex-session", "codex-session"]
    assert [row["surface"] for row in events] == ["loop", "loop"]


def test_codex_sync_discovers_isolated_loop_home(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".loop" / "runs" / "project" / "33" / "codex-home" / "sessions" / "2026" / "07" / "24"
    root.mkdir(parents=True)
    _write_rows(root / "loop.jsonl", [
        {
            "timestamp": (now - timedelta(seconds=2)).isoformat(),
            "type": "session_meta",
            "payload": {"id": "loop-session", "originator": "loop", "source": "vscode"},
        },
        {
            "timestamp": (now - timedelta(seconds=1)).isoformat(),
            "payload": {"type": "turn_context", "model": "gpt-5.3-codex-spark"},
        },
        {
            "timestamp": now.isoformat(),
            "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 800,
                "output_tokens": 50,
            }}},
        },
    ])

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = usage_ledger.sync_provider("codex")
        events = usage_ledger.events_between(
            "codex", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert result["files"] == 1
    assert len(events) == 1
    assert events[0]["surface"] == "loop"
    assert events[0]["model"] == "gpt-5.3-codex-spark"
    assert events[0]["input_tokens"] == 200
    assert events[0]["cache_read_tokens"] == 800


def test_codex_restores_use_run_model_and_dedupe_cumulative_token_events(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    home = tmp_path / ".loop" / "runs" / "project" / "27" / "codex-home"
    (home / "config.toml").parent.mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    info = {
        "last_token_usage": {"input_tokens": 100, "output_tokens": 20},
        "total_token_usage": {"input_tokens": 500, "output_tokens": 100},
        "model_context_window": 100000,
    }
    for index in (1, 2):
        root = home / "sessions" / "2026" / "07" / "24"
        root.mkdir(parents=True, exist_ok=True)
        _write_rows(root / f"restore-{index}.jsonl", [
            {
                "timestamp": (now + timedelta(seconds=index)).isoformat(),
                "type": "session_meta",
                "payload": {"id": "restored-session", "originator": "loop", "source": "vscode"},
            },
            {
                "timestamp": (now + timedelta(seconds=index)).isoformat(),
                "payload": {"type": "token_count", "info": info},
            },
        ])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("codex")
        events = usage_ledger.events_between(
            "codex", now - timedelta(minutes=1), now + timedelta(minutes=1)
        )

    assert [event["model"] for event in events] == ["gpt-5.6-sol", "gpt-5.6-sol"]
    assert sum(event["input_tokens"] for event in events) == 100
    assert sum(event["output_tokens"] for event in events) == 20


def test_daily_model_aggregates_rebuild_for_affected_days(tmp_path):
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "project"
    root.mkdir(parents=True)
    session_file = root / "daily.jsonl"
    _write_rows(session_file, [
        _claude_event(now - timedelta(minutes=2), "one"),
        _claude_event(now - timedelta(minutes=1), "two", 200),
    ])

    with patch("pathlib.Path.home", return_value=tmp_path):
        usage_ledger.sync_provider("claude")
        rows = usage_ledger.daily_models("claude", "2026-07-24", "2026-07-24")

    assert len(rows) == 1
    assert rows[0]["model"] == "claude-fable-5"
    assert rows[0]["messages"] == 2
    assert rows[0]["sessions"] == 1
    assert rows[0]["requests"] == 2
    assert rows[0]["input_tokens"] == 300
    assert rows[0]["output_tokens"] == 100


def test_init_migrates_legacy_cursor_table(tmp_path):
    db_path = tmp_path / "activity.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE usage_file_cursors(
                   provider TEXT NOT NULL,
                   path TEXT NOT NULL,
                   file_key TEXT NOT NULL,
                   device INTEGER NOT NULL,
                   inode INTEGER NOT NULL,
                   offset_bytes INTEGER NOT NULL,
                   size_bytes INTEGER NOT NULL,
                   current_model TEXT NOT NULL DEFAULT 'unknown',
                   session_id TEXT,
                   updated_at INTEGER NOT NULL,
                   PRIMARY KEY(provider, path)
               )"""
        )
        conn.commit()

    usage_ledger.init()

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(usage_file_cursors)").fetchall()
        }
    assert {"cursor_hash", "cwd", "current_surface", "parser_state_json"}.issubset(columns)
    with sqlite3.connect(db_path) as conn:
        event_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()
        }
    assert {
        "cwd", "surface", "usage_breakdown_json", "compaction_generation"
    }.issubset(event_columns)


def test_init_reindexes_usage_when_explanation_marker_is_missing(tmp_path):
    usage_ledger.init()
    with sqlite3.connect(tmp_path / "activity.db") as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day,
                   session_id, model
               ) VALUES ('claude', 'old', 0, '/old', 1, '2026-07-24',
                         'old-session', 'claude-old')"""
        )
        conn.execute(
            "DELETE FROM usage_ledger_meta WHERE key = 'usage_explanation_v2'"
        )
        conn.commit()
    usage_ledger._initialized_paths.discard(str(tmp_path / "activity.db"))

    usage_ledger.init()

    with sqlite3.connect(tmp_path / "activity.db") as conn:
        count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        marker = conn.execute(
            "SELECT value FROM usage_ledger_meta WHERE key = 'usage_explanation_v2'"
        ).fetchone()
    assert count == 0
    assert marker == ("1",)


def test_init_reindexes_codex_when_attribution_backfill_marker_is_missing(tmp_path):
    usage_ledger.init()
    with sqlite3.connect(tmp_path / "activity.db") as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day,
                   session_id, model, surface
               ) VALUES ('codex', 'old', 0, '/old', 1, '2026-07-24',
                         'old-session', 'gpt-old', 'unknown')"""
        )
        conn.execute(
            "DELETE FROM usage_ledger_meta WHERE key = 'codex_attribution_backfill_v2'"
        )
        conn.commit()

    usage_ledger._initialized_paths.discard(str(tmp_path / "activity.db"))
    usage_ledger.init()

    with sqlite3.connect(tmp_path / "activity.db") as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE provider = 'codex'"
        ).fetchone()[0]
        marker = conn.execute(
            "SELECT value FROM usage_ledger_meta WHERE key = 'codex_attribution_backfill_v2'"
        ).fetchone()[0]
    assert remaining == 0
    assert marker == "1"


def test_init_reindexes_claude_when_dedupe_backfill_marker_is_missing(tmp_path):
    usage_ledger.init()
    with sqlite3.connect(tmp_path / "activity.db") as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day,
                   session_id, model, surface
               ) VALUES ('claude', 'old', 0, '/old', 1, '2026-07-24',
                         'old-session', 'claude-old', 'unknown')"""
        )
        conn.execute(
            "DELETE FROM usage_ledger_meta WHERE key = 'claude_attribution_dedupe_v1'"
        )
        conn.commit()

    usage_ledger._initialized_paths.discard(str(tmp_path / "activity.db"))
    usage_ledger.init()

    with sqlite3.connect(tmp_path / "activity.db") as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE provider = 'claude'"
        ).fetchone()[0]
        marker = conn.execute(
            "SELECT value FROM usage_ledger_meta WHERE key = 'claude_attribution_dedupe_v1'"
        ).fetchone()[0]
    assert remaining == 0
    assert marker == "1"
