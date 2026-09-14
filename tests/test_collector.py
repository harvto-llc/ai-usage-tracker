"""Tests for collector functions with temp files and mocked dependencies."""

import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.collector import (
    _cached_usage_for_snapshots,
    _claude_usage_source,
    _scrape_claude_usage,
    scan_codex_sessions,
    _should_scrape_usage,
    USAGE_STAMP,
)
from src.database import load_claude_code_stats
from src.provider_metrics import scan_codex_thread_stats, shape_claude_code_stats


class TestScanCodexSessions:
    def test_no_sessions_dir(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_sessions()
        assert result["messages_today"] is None
        assert result["sessions_today"] is None
        assert result["total_sessions"] is None

    def test_with_session_files(self, tmp_path):
        now = datetime.now(timezone.utc)
        # glob pattern is */*/*/*.jsonl — 4 levels under sessions/
        sessions_dir = tmp_path / ".codex" / "sessions" / "a" / "b" / "c"
        sessions_dir.mkdir(parents=True)
        jsonl = sessions_dir / "events.jsonl"

        lines = [
            json.dumps({"timestamp": now.isoformat(), "payload": {"role": "user"}}),
            json.dumps({"timestamp": now.isoformat(), "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 100, "output_tokens": 50, "reasoning_output_tokens": 10, "cached_input_tokens": 20}
            }}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_sessions()
        assert result["messages_today"] == 1
        assert result["sessions_today"] == 1
        assert result["total_sessions"] == 1
        assert result["events_scanned"] == 2
        assert result["input_tokens_today"] == 100
        assert result["output_tokens_today"] == 50
        assert result["reasoning_tokens_today"] == 10
        assert "messages" not in result
        assert "sessions" not in result
        assert "input_tokens" not in result

    def test_includes_isolated_loop_session_files(self, tmp_path):
        now = datetime.now(timezone.utc)
        sessions_dir = (
            tmp_path / ".loop" / "runs" / "project" / "33" / "codex-home"
            / "sessions" / "2026" / "07" / "25"
        )
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "events.jsonl").write_text(json.dumps({
            "timestamp": now.isoformat(),
            "payload": {"role": "user"},
        }) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_sessions()

        assert result["messages_today"] == 1
        assert result["sessions_today"] == 1
        assert result["total_sessions"] == 1

    def test_old_entries_excluded(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=2)
        sessions_dir = tmp_path / ".codex" / "sessions" / "2026" / "04" / "07"
        sessions_dir.mkdir(parents=True)
        jsonl = sessions_dir / "events.jsonl"
        jsonl.write_text(json.dumps({"timestamp": old.isoformat(), "payload": {"role": "user"}}) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_sessions()
        assert result["messages_today"] == 0
        assert result["sessions_today"] == 0
        assert result["total_sessions"] == 1

    def test_token_count_with_null_info_does_not_crash(self, tmp_path):
        now = datetime.now(timezone.utc)
        sessions_dir = tmp_path / ".codex" / "sessions" / "a" / "b" / "c"
        sessions_dir.mkdir(parents=True)
        jsonl = sessions_dir / "events.jsonl"

        lines = [
            json.dumps({"timestamp": now.isoformat(), "payload": {"type": "token_count", "info": None}}),
            json.dumps({"timestamp": now.isoformat(), "payload": {"role": "user"}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_sessions()
        assert result["messages_today"] == 1
        assert result["events_scanned"] == 2
        assert result["input_tokens_today"] == 0
        assert result["output_tokens_today"] == 0


class TestScanCodexLocal:
    def test_no_db(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_thread_stats()
        assert result["total_threads"] is None
        assert result["today_threads"] is None

    def test_with_db(self, tmp_path):
        db_path = tmp_path / ".codex" / "state_5.sqlite"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE threads(
            title TEXT, tokens_used INTEGER, model TEXT, source TEXT, updated_at INTEGER
        )""")
        now = int(time.time())
        conn.execute("INSERT INTO threads VALUES(?, ?, ?, ?, ?)",
                     ("Fix bug", 5000, "o4-mini", "cli", now))
        conn.execute("INSERT INTO threads VALUES(?, ?, ?, ?, ?)",
                     ("Add feature", 3000, "o4-mini", "cli", now - 86400 * 2))
        conn.commit()
        conn.close()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = scan_codex_thread_stats()
        assert result is not None
        assert result["total_tokens"] == 8000
        assert result["total_threads"] == 2
        assert result["today_tokens"] == 5000
        assert result["today_threads"] == 1
        assert len(result["recent_threads"]) == 2
        assert "by_source" in result
        assert result["by_source"]["cli"]["sessions"] == 2


class TestScanCCStats:
    def test_no_file(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = shape_claude_code_stats(load_claude_code_stats())
        assert result["total_sessions"] is None
        assert result["daily_activity"] == []

    def test_with_stats(self, tmp_path):
        stats_dir = tmp_path / ".claude"
        stats_dir.mkdir(parents=True)
        stats_file = stats_dir / "stats-cache.json"
        stats_file.write_text(json.dumps({
            "totalSessions": 42,
            "totalMessages": 1000,
            "modelUsage": ["claude-opus-4-5"],
            "longestSession": {"duration": 7200000},
            "dailyActivity": [
                {"date": "2026-04-01", "messageCount": 50, "sessionCount": 3, "toolCallCount": 20},
            ],
            "dailyModelTokens": [
                {"date": "2026-04-01", "tokensByModel": {"claude-opus-4-5": 5000}},
            ],
            "hourCounts": {"10": 5, "14": 8},
        }))

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = shape_claude_code_stats(load_claude_code_stats())
        assert result["total_sessions"] == 42
        assert result["favorite_model"] == "Opus 4 5"
        assert result["longest_session"] == "2h 0m"
        assert len(result["daily_activity"]) == 1

    def test_bad_json(self, tmp_path):
        stats_dir = tmp_path / ".claude"
        stats_dir.mkdir(parents=True)
        (stats_dir / "stats-cache.json").write_text("not json{")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = shape_claude_code_stats(load_claude_code_stats())
        assert result["total_sessions"] is None


class TestShouldScrapeUsage:
    def test_no_stamp(self, tmp_path):
        stamp = tmp_path / "stamp"
        with patch("src.collector.USAGE_STAMP", stamp):
            assert _should_scrape_usage() is True

    def test_recent_stamp(self, tmp_path):
        stamp = tmp_path / "stamp"
        stamp.write_text("{}")
        with patch("src.collector.USAGE_STAMP", stamp):
            assert _should_scrape_usage() is False

    def test_old_stamp(self, tmp_path):
        import os
        stamp = tmp_path / "stamp"
        stamp.write_text("{}")
        old_time = time.time() - 600
        os.utime(stamp, (old_time, old_time))
        with patch("src.collector.USAGE_STAMP", stamp):
            assert _should_scrape_usage() is True

    def test_cached_quota_is_available_for_fast_cycle_snapshots(self, tmp_path):
        stamp = tmp_path / "stamp"
        stamp.write_text(json.dumps({
            "usage": {"session_pct": 12, "plan_id": "max-5x"},
            "codex_usage": {"weekly_remaining_pct": 19, "plan_id": "pro"},
        }))

        with patch("src.collector.USAGE_STAMP", stamp):
            usage, codex_usage = _cached_usage_for_snapshots()

        assert usage["session_pct"] == 12
        assert codex_usage["weekly_remaining_pct"] == 19


class TestClaudeUsageSource:
    def test_invalid_source_defaults_to_auto(self):
        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "weird"}, clear=False):
            assert _claude_usage_source() == "auto"

    def test_tmux_source_no_longer_supported(self):
        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "tmux"}, clear=False):
            assert _claude_usage_source() == "auto"

    @patch("src.collector.scrape_claude_usage_web")
    @patch("src.collector.claude_web_usage_configured", return_value=True)
    def test_auto_uses_web(self, mock_configured, mock_web):
        mock_web.return_value = {"session_pct": 24, "weekly_pct": 17}

        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "auto"}, clear=False):
            usage, source = _scrape_claude_usage()

        assert source == "web"
        assert usage["session_pct"] == 24

    @patch("src.collector.scrape_claude_usage_web", return_value=None)
    @patch("src.collector.claude_web_usage_configured", return_value=True)
    def test_auto_without_web_data_returns_none(self, mock_configured, mock_web):
        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "auto"}, clear=False):
            usage, source = _scrape_claude_usage()

        assert usage is None
        assert source == "web"
        mock_web.assert_called_once()

    @patch("src.collector.scrape_claude_usage_web")
    @patch("src.collector.claude_web_usage_configured", return_value=False)
    def test_auto_unconfigured_never_scrapes(self, mock_configured, mock_web):
        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "auto"}, clear=False):
            usage, source = _scrape_claude_usage()

        assert usage is None
        assert source == "web"
        mock_web.assert_not_called()

    @patch("src.collector.scrape_claude_usage_web", return_value=None)
    @patch("src.collector.claude_web_usage_configured", return_value=False)
    def test_web_source_scrapes_even_unconfigured(self, mock_configured, mock_web):
        with patch.dict("os.environ", {"CLAUDE_USAGE_SOURCE": "web"}, clear=False):
            usage, source = _scrape_claude_usage()

        assert usage is None
        assert source == "web"
        mock_web.assert_called_once()


class TestCollectorAccess:
    """API-based access skips quota scrapes while local scans still run."""

    def test_legacy_mode_names_normalize_to_access_types(self):
        from src.collector import _normalize_access

        assert _normalize_access("full") == "subscription"
        assert _normalize_access("local") == "api"
        assert _normalize_access("api-based") == "api"

    def test_access_env_wins_over_legacy_mode_env(self):
        from src.collector import _default_access

        with patch.dict(
            "os.environ",
            {"USAGE_TRACKER_ACCESS": "api", "USAGE_TRACKER_MODE": "full"},
            clear=False,
        ):
            assert _default_access() == "api"

    @patch("src.collector.urllib.request.urlopen")
    @patch("src.collector._scrape_claude_usage")
    @patch("src.collector.scrape_codex_usage")
    @patch("src.collector.scrape_cursor_usage")
    @patch("src.collector._should_scrape_usage", return_value=True)
    @patch("src.collector.shape_claude_code_stats", return_value={})
    @patch("src.collector.load_claude_code_stats", return_value={})
    @patch("src.collector.scan_codex_sessions", return_value={})
    @patch("src.collector.scan_codex_thread_stats", return_value={})
    @patch("src.collector.scan_cc_projects_today", return_value=[])
    @patch("src.collector.scan_cc_tokens_today", return_value={"total_tokens": 0})
    @patch("src.collector.scan_cc_messages_today", return_value={"total_messages": 0})
    @patch("src.collector.build_provider_snapshots", return_value={})
    def test_api_access_skips_scrapes(
        self, mock_snapshots, mock_msgs, mock_tokens, mock_projects,
        mock_threads, mock_sessions, mock_load, mock_shape,
        mock_should, mock_cursor, mock_codex, mock_claude, mock_urlopen,
    ):
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        from src.collector import main

        main(access="api")
        mock_claude.assert_not_called()
        mock_codex.assert_not_called()
        mock_cursor.assert_not_called()
        # local scans still ran and the payload was posted
        mock_msgs.assert_called_once()
        assert mock_snapshots.call_args.kwargs["collector_access"] == "api"
        mock_urlopen.assert_called_once()

    @patch("src.collector.urllib.request.urlopen")
    @patch("src.collector._scrape_claude_usage", return_value=(None, "tmux"))
    @patch("src.collector.scrape_codex_usage", return_value=None)
    @patch("src.collector.scrape_cursor_usage", return_value=None)
    @patch("src.collector.codex_web_analytics_configured", return_value=False)
    @patch("src.collector.sync")
    @patch("src.collector._should_scrape_usage", return_value=True)
    @patch("src.collector.shape_claude_code_stats", return_value={})
    @patch("src.collector.load_claude_code_stats", return_value={})
    @patch("src.collector.scan_codex_sessions", return_value={})
    @patch("src.collector.scan_codex_thread_stats", return_value={})
    @patch("src.collector.scan_cc_projects_today", return_value=[])
    @patch("src.collector.scan_cc_tokens_today", return_value={"total_tokens": 0})
    @patch("src.collector.scan_cc_messages_today", return_value={"total_messages": 0})
    @patch("src.collector.build_provider_snapshots", return_value={})
    def test_subscription_access_runs_scrapes(
        self, mock_snapshots, mock_msgs, mock_tokens, mock_projects,
        mock_threads, mock_sessions, mock_load, mock_shape, mock_should,
        mock_sync, mock_analytics_cfg, mock_cursor, mock_codex, mock_claude,
        mock_urlopen, tmp_path,
    ):
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        from src.collector import main

        with patch("src.collector.USAGE_STAMP", tmp_path / "stamp"):
            main(access="subscription")
        mock_claude.assert_called_once()
        mock_codex.assert_called_once()
        mock_cursor.assert_called_once()
        assert mock_snapshots.call_args.kwargs["collector_access"] == "subscription"
