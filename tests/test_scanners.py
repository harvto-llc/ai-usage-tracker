"""Tests for scanners with temporary JSONL files."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scanners import (
    scan_cc_messages_today,
    scan_cc_tokens_today,
    scan_cc_daily_model_tokens,
    scan_cc_projects_today,
    _active_hours_from_timestamps,
    _today_boundaries,
)


def _make_jsonl_entry(
    dt: datetime, role="assistant", model="claude-opus-4-5",
    usage=None, content=None, message_id=None, event_uuid=None, session_id=None,
):
    entry = {
        "timestamp": dt.isoformat(),
        "message": {"role": role},
    }
    if event_uuid:
        entry["uuid"] = event_uuid
    if session_id:
        entry["sessionId"] = session_id
    if content is not None:
        entry["message"]["content"] = content
    if usage:
        entry["message"]["usage"] = usage
        entry["message"]["model"] = model
    if message_id:
        entry["message"]["id"] = message_id
    return json.dumps(entry)


@pytest.fixture
def mock_home(tmp_path):
    """Create a temporary home directory with JSONL project files."""
    # Use local noon today (guaranteed to be within today's boundaries regardless of timezone)
    start_utc, _ = _today_boundaries()
    midday = start_utc + timedelta(hours=6)

    project_dir = tmp_path / ".claude" / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "session.jsonl"

    lines = [
        _make_jsonl_entry(midday, role="user"),
        _make_jsonl_entry(midday + timedelta(minutes=5), role="assistant",
                          model="claude-opus-4-5",
                          usage={"input_tokens": 100, "output_tokens": 50,
                                 "cache_read_input_tokens": 80,
                                 "cache_creation_input_tokens": 20}),
        _make_jsonl_entry(midday + timedelta(hours=1), role="user"),
        _make_jsonl_entry(midday + timedelta(hours=1, minutes=5), role="assistant",
                          model="claude-sonnet-4-5",
                          usage={"input_tokens": 200, "output_tokens": 100,
                                 "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 50}),
        _make_jsonl_entry(midday + timedelta(hours=2), role="user"),
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    return tmp_path


class TestScanCCMessagesToday:
    def test_with_data(self, mock_home):
        with patch("pathlib.Path.home", return_value=mock_home):
            result = scan_cc_messages_today()
        assert result["total_messages"] == 5
        assert result["conversations"] == 1
        assert result["active_hours"] >= 0

    def test_no_files(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_messages_today()
        assert result["total_messages"] == 0
        assert result["active_hours"] == 0.0

    def test_bad_json(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test"
        project_dir.mkdir(parents=True)
        (project_dir / "bad.jsonl").write_text("not json\n{bad\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_messages_today()
        assert result["total_messages"] == 0

    def test_old_entries_excluded(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test"
        project_dir.mkdir(parents=True)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        (project_dir / "old.jsonl").write_text(_make_jsonl_entry(old, role="user") + "\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_messages_today()
        assert result["total_messages"] == 0

    def test_local_command_entries_excluded(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test"
        project_dir.mkdir(parents=True)
        start_utc, _ = _today_boundaries()
        today = start_utc + timedelta(hours=10)
        entries = [
            _make_jsonl_entry(today, role="user", content="<local-command-caveat>generated</local-command-caveat>"),
            _make_jsonl_entry(today + timedelta(minutes=1), role="user", content="<command-name>/exit</command-name>"),
            _make_jsonl_entry(today + timedelta(minutes=2), role="user", content="<local-command-stdout>Goodbye!</local-command-stdout>"),
            _make_jsonl_entry(today + timedelta(minutes=3), role="user", content="Real prompt"),
            _make_jsonl_entry(today + timedelta(minutes=4), role="assistant", content="Real response"),
        ]
        (project_dir / "session.jsonl").write_text("\n".join(entries) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_messages_today()

        assert result["total_messages"] == 2
        assert result["conversations"] == 1
        assert result["by_hour"] == {today.astimezone().strftime("%H"): 2}


class TestScanCCTokensToday:
    def test_with_data(self, mock_home):
        with patch("pathlib.Path.home", return_value=mock_home):
            result = scan_cc_tokens_today()
        assert result["total_tokens"] > 0
        assert result["output_tokens"] == 150
        assert result["input_tokens"] == 300
        assert result["cache_read_tokens"] == 80
        assert "by_model" in result
        assert len(result["by_model"]) == 2

    def test_no_data(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_tokens_today()
        assert result["total_tokens"] == 0

    def test_repeated_content_records_count_message_usage_once(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test"
        project_dir.mkdir(parents=True)
        start_utc, _ = _today_boundaries()
        usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}
        rows = [
            _make_jsonl_entry(
                start_utc + timedelta(hours=1, seconds=offset),
                usage=usage,
                message_id="msg_same_response",
            )
            for offset in range(3)
        ]
        (project_dir / "session.jsonl").write_text("\n".join(rows) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            today = scan_cc_tokens_today()
            daily = scan_cc_daily_model_tokens(days=1)

        assert today["input_tokens"] == 100
        assert today["output_tokens"] == 20
        assert today["cache_read_tokens"] == 50
        assert daily[0]["tokens_by_model"] == {"claude-opus-4-5": 120}

    def test_forked_files_dedupe_messages_tokens_and_conversations(self, tmp_path):
        start_utc, _ = _today_boundaries()
        rows = [
            _make_jsonl_entry(
                start_utc + timedelta(hours=1),
                role="user",
                event_uuid="user-event",
                session_id="shared-session",
            ),
            _make_jsonl_entry(
                start_utc + timedelta(hours=1, minutes=1),
                usage={"input_tokens": 100, "output_tokens": 20},
                message_id="msg_shared_response",
                event_uuid="assistant-event",
                session_id="shared-session",
            ),
        ]
        for project in ("original", "fork"):
            root = tmp_path / ".claude" / "projects" / project
            root.mkdir(parents=True)
            (root / "session.jsonl").write_text("\n".join(rows) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            messages = scan_cc_messages_today()
            tokens = scan_cc_tokens_today()
            daily = scan_cc_daily_model_tokens(days=1)

        assert messages["total_messages"] == 2
        assert messages["conversations"] == 1
        assert tokens["input_tokens"] == 100
        assert tokens["output_tokens"] == 20
        assert daily[0]["tokens_by_model"] == {"claude-opus-4-5": 120}


class TestScanCCDailyModelTokens:
    def test_recent_daily_model_tokens(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        today = datetime.now(timezone.utc)
        yesterday = today - timedelta(days=1)
        old = today - timedelta(days=45)
        (project_dir / "session.jsonl").write_text("\n".join([
            _make_jsonl_entry(today, role="assistant", model="claude-sonnet-4-6",
                              usage={"input_tokens": 100, "output_tokens": 40}),
            _make_jsonl_entry(today, role="assistant", model="claude-sonnet-4-6",
                              usage={"input_tokens": 10, "output_tokens": 5,
                                     "cache_read_input_tokens": 999}),
            _make_jsonl_entry(yesterday, role="assistant", model="claude-opus-4-6",
                              usage={"input_tokens": 20, "output_tokens": 7}),
            _make_jsonl_entry(old, role="assistant", model="claude-opus-4-6",
                              usage={"input_tokens": 999, "output_tokens": 999}),
            _make_jsonl_entry(today, role="assistant", model="<synthetic>",
                              usage={"input_tokens": 0, "output_tokens": 0}),
        ]) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_daily_model_tokens(days=30)

        by_date = {entry["date"]: entry["tokens_by_model"] for entry in result}
        assert yesterday.astimezone().date().isoformat() in by_date
        assert by_date[today.astimezone().date().isoformat()] == {"claude-sonnet-4-6": 155}
        assert old.astimezone().date().isoformat() not in by_date


class TestScanCCProjectsToday:
    def test_with_data(self, mock_home):
        with patch("pathlib.Path.home", return_value=mock_home):
            result = scan_cc_projects_today()
        assert len(result) == 1
        assert result[0]["messages"] == 5

    def test_no_data(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_projects_today()
        assert result == []

    def test_local_command_entries_excluded(self, tmp_path):
        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        start_utc, _ = _today_boundaries()
        today = start_utc + timedelta(hours=10)
        entries = [
            _make_jsonl_entry(today, role="user", content="<command-name>/exit</command-name>"),
            _make_jsonl_entry(today + timedelta(minutes=1), role="user", content="Real prompt"),
            _make_jsonl_entry(today + timedelta(minutes=2), role="assistant", content="Real response"),
        ]
        (project_dir / "session.jsonl").write_text("\n".join(entries) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_cc_projects_today()

        assert len(result) == 1
        assert result[0]["messages"] == 2


class TestClaudeJsonlRoots:
    def test_includes_cli_and_desktop_session_files(self, tmp_path):
        cli = tmp_path / ".claude" / "projects" / "proj-a"
        cli.mkdir(parents=True)
        (cli / "s1.jsonl").write_text("{}\n")

        desktop = (
            tmp_path / "Library" / "Application Support" / "Claude"
            / "local-agent-mode-sessions" / "uuid1" / "uuid2" / "local_x"
            / ".claude" / "projects" / "-sessions-foo"
        )
        desktop.mkdir(parents=True)
        (desktop / "s2.jsonl").write_text("{}\n")

        from src import scanners
        with patch("pathlib.Path.home", return_value=tmp_path):
            files = scanners.claude_jsonl_files()

        names = sorted(Path(f).name for f in files)
        assert names == ["s1.jsonl", "s2.jsonl"]
