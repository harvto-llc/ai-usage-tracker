"""Tests for API helpers and scanner utilities."""

import os

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from datetime import datetime
from src.api import (
    compute_risk_outlook,
    normalize_claude_session_reset,
    normalize_reset,
    parse_claude_session_hours_left,
    parse_hours_left,
)
from src.scanners import _active_hours_from_timestamps, _today_boundaries


# ── normalize_reset ──────────────────────────────────────

class TestParseHoursLeft:
    def test_relative_session_reset(self):
        assert parse_hours_left("in 4 hr 52 min") == 4.9


class TestClaudeSessionReset:
    def test_past_time_only_is_unknown_not_tomorrow(self):
        now = datetime(2026, 7, 5, 14, 29)

        assert parse_claude_session_hours_left("2:20pm", now=now) is None
        assert normalize_claude_session_reset("2:20pm", now=now) is None

    def test_relative_reset_is_preserved_and_counted(self):
        assert parse_claude_session_hours_left("in 4 hr 52 min") == 4.9
        assert normalize_claude_session_reset("in 4 hr 52 min") == "in 4 hr 52 min"

    def test_future_time_only_inside_rolling_window_is_absolute(self):
        now = datetime(2026, 7, 5, 14, 29)

        assert parse_claude_session_hours_left("7:19pm", now=now) == 4.8
        assert normalize_claude_session_reset("7:19pm", now=now) == "Jul 5 7:19 PM"


class TestNormalizeReset:
    def test_none(self):
        assert normalize_reset(None) is None

    def test_empty(self):
        assert normalize_reset("") == ""

    def test_full_date(self):
        result = normalize_reset("Apr 14 12:00 AM")
        assert result is not None
        assert "Apr" in result
        assert "14" in result
        assert "12:00 AM" in result

    def test_time_only(self):
        result = normalize_reset("1am")
        assert result is not None
        assert "1:00 AM" in result

    def test_already_formatted(self):
        result = normalize_reset("Apr 9 3:17 AM")
        assert result is not None
        assert "3:17 AM" in result

    def test_at_format(self):
        result = normalize_reset("Apr 7 at 12am")
        assert result is not None
        assert "12:00 AM" in result

    def test_weekly_absolute_at_format_still_normalizes(self):
        assert normalize_reset("Jul 6 at 5:59pm") == "Jul 6 5:59 PM"


# ── compute_risk_outlook ─────────────────────────────────

class TestRiskOutlook:
    def test_all_clear(self):
        result = compute_risk_outlook(0, 0, {}, {})
        assert result == "All clear"

    def test_claude_session_risk(self):
        result = compute_risk_outlook(60, 95, {}, {})
        assert "⚠" in result
        assert "Claude session" in result

    def test_no_risk_with_pace(self):
        pace = {"pace_status": "on_track", "projected_pct": 80, "days_remaining": 3.0}
        result = compute_risk_outlook(5, 30, pace, {})
        assert "Claude week" in result
        assert "80%" in result

    def test_front_loaded(self):
        pace = {"pace_status": "front_loaded", "projected_pct": 120, "days_remaining": 2.0}
        result = compute_risk_outlook(5, 50, pace, {})
        assert "⚠" in result
        assert "front-loaded" in result


# ── _active_hours_from_timestamps ────────────────────────

class TestActiveHours:
    def test_empty(self):
        assert _active_hours_from_timestamps([]) == 0.0

    def test_single(self):
        assert _active_hours_from_timestamps([datetime(2026, 1, 1, 10, 0)]) == 0.0

    def test_two_close(self):
        ts = [datetime(2026, 1, 1, 10, 0), datetime(2026, 1, 1, 10, 30)]
        result = _active_hours_from_timestamps(ts)
        assert abs(result - 0.5) < 0.01

    def test_gap_splits_blocks(self):
        ts = [
            datetime(2026, 1, 1, 10, 0),
            datetime(2026, 1, 1, 10, 15),
            # 45-min gap (> 30 min threshold)
            datetime(2026, 1, 1, 11, 0),
            datetime(2026, 1, 1, 11, 30),
        ]
        result = _active_hours_from_timestamps(ts)
        # Block 1: 15 min = 0.25h, Block 2: 30 min = 0.5h
        assert abs(result - 0.75) < 0.01

    def test_continuous(self):
        ts = [
            datetime(2026, 1, 1, 10, 0),
            datetime(2026, 1, 1, 10, 10),
            datetime(2026, 1, 1, 10, 20),
            datetime(2026, 1, 1, 10, 25),
        ]
        result = _active_hours_from_timestamps(ts)
        # 25 min continuous
        assert abs(result - 25/60) < 0.01


# ── _today_boundaries ───────────────────────────────────

class TestTodayBoundaries:
    def test_returns_tuple(self):
        start, end = _today_boundaries()
        assert start < end
        assert (end - start).total_seconds() == 86400
