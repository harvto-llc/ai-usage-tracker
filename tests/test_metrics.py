"""Tests for metrics calculations."""

import time

import pytest
from unittest.mock import patch
from src.metrics import (
    codex_burn_rate,
    current_streak,
    daily_activity,
    predict_lock,
    session_burn_rate,
    weekly_utilization_pace,
    workload_label,
    output_density,
    cache_health,
    _compute_active_mask,
    _estimate_days_until_reset,
)


# ── workload_label ───────────────────────────────────────

class TestWorkloadLabel:
    def test_chat(self):
        assert workload_label(2.0) == "Chat"

    def test_coding(self):
        assert workload_label(8.0) == "Coding"

    def test_heavy(self):
        assert workload_label(20.0) == "Heavy Code"

    def test_ultra(self):
        assert workload_label(40.0) == "Ultra Context"

    def test_zero(self):
        assert workload_label(0.0) == "Chat"


# ── output_density ───────────────────────────────────────

class TestOutputDensity:
    def test_normal(self):
        assert output_density(50000, 2.0) == 25000

    def test_zero_hours(self):
        assert output_density(50000, 0.0) is None

    def test_zero_tokens(self):
        assert output_density(0, 5.0) == 0


# ── cache_health ─────────────────────────────────────────

class TestCacheHealth:
    def test_all_read(self):
        assert cache_health(100, 0) == 100.0

    def test_all_create(self):
        assert cache_health(0, 100) == 0.0

    def test_half(self):
        assert cache_health(50, 50) == 50.0

    def test_zero(self):
        assert cache_health(0, 0) == 0.0

    def test_high_ratio(self):
        result = cache_health(950, 50)
        assert result == 95.0


# ── _estimate_days_until_reset ───────────────────────────

class TestEstimateDaysUntilReset:
    def test_none(self):
        assert _estimate_days_until_reset("") is None

    def test_day_name(self):
        result = _estimate_days_until_reset("Tue 12:00 AM")
        assert result is not None
        assert 0 < result <= 7

    def test_date_format(self):
        result = _estimate_days_until_reset("Apr 14")
        assert result is not None
        assert result >= 0

    def test_garbage(self):
        assert _estimate_days_until_reset("not a date") is None


# ── session_burn_rate ────────────────────────────────────

class TestSessionBurnRate:
    @patch("src.metrics.last_samples")
    def test_no_samples(self, mock):
        mock.return_value = []
        assert session_burn_rate() == 0.0

    @patch("src.metrics.last_samples")
    def test_one_sample(self, mock):
        mock.return_value = [(1000, 50, 20, 0)]
        assert session_burn_rate() == 0.0

    @patch("src.metrics.last_samples")
    def test_increasing(self, mock):
        # 10% increase over 1 hour
        mock.return_value = [
            (3600 + 1000, 60, 20, 0),  # newest
            (1000, 50, 20, 0),          # oldest
        ]
        result = session_burn_rate()
        assert abs(result - 10.0) < 0.1

    @patch("src.metrics.last_samples")
    def test_decreasing(self, mock):
        # Session reset — should return 0
        mock.return_value = [
            (3600 + 1000, 10, 20, 0),  # newest (reset)
            (1000, 80, 20, 0),          # oldest
        ]
        assert session_burn_rate() == 0.0


# ── codex_burn_rate ──────────────────────────────────────

class TestCodexBurnRate:
    @patch("src.metrics.last_codex_samples")
    def test_no_samples(self, mock):
        mock.return_value = []
        assert codex_burn_rate() == 0.0

    @patch("src.metrics.last_codex_samples")
    def test_one_sample(self, mock):
        mock.return_value = [(1000, 90)]
        assert codex_burn_rate() == 0.0

    @patch("src.metrics.last_codex_samples")
    def test_burning(self, mock):
        mock.return_value = [
            (4600, 80),  # newest: 80% remaining
            (1000, 90),  # oldest: 90% remaining
        ]
        rate = codex_burn_rate()
        assert rate == pytest.approx(10.0, abs=0.5)

    @patch("src.metrics.last_codex_samples")
    def test_no_change(self, mock):
        mock.return_value = [(4600, 90), (1000, 90)]
        assert codex_burn_rate() == 0.0


# ── predict_lock ─────────────────────────────────────────

class TestPredictLock:
    @patch("src.metrics.session_burn_rate", return_value=0)
    def test_no_burn(self, mock):
        assert predict_lock(50) is None

    @patch("src.metrics.session_burn_rate", return_value=10)
    def test_with_burn(self, mock):
        eta = predict_lock(50)
        assert eta is not None
        assert eta > time.time()


# ── weekly_utilization_pace ──────────────────────────────

class TestWeeklyUtilizationPace:
    @patch("src.metrics.latest_sample", return_value=None)
    def test_no_data(self, mock):
        result = weekly_utilization_pace()
        assert result["projected_pct"] == 0

    @patch("src.metrics.last_samples", return_value=[])
    @patch("src.metrics.latest_sample")
    def test_with_data(self, mock, mock_samples):
        mock.return_value = {
            "weekly": 50, "weekly_reset": "Tue 12:00 AM",
            "session": 30, "extra": 0,
            "timestamp": int(time.time()),
        }
        result = weekly_utilization_pace()
        assert "projected_pct" in result
        assert "pace_status" in result
        assert result["pace_status"] in ("under", "on_track", "front_loaded")

    @patch("src.metrics.last_samples", return_value=[])
    @patch("src.metrics.latest_sample")
    def test_no_reset_string(self, mock, mock_samples):
        mock.return_value = {"weekly": 50, "weekly_reset": "", "session": 30, "extra": 0, "timestamp": int(time.time())}
        result = weekly_utilization_pace()
        assert result["days_elapsed"] == 3.5  # fallback


# ── current_streak ───────────────────────────────────────

class TestCurrentStreak:
    @patch("src.metrics.daily_activity")
    def test_no_data(self, mock):
        mock.return_value = []
        assert current_streak() == 0

    @patch("src.metrics.daily_activity")
    def test_all_active(self, mock):
        mock.return_value = [
            {"date": "2026-04-07", "active_hours": 2},
            {"date": "2026-04-08", "active_hours": 1.5},
            {"date": "2026-04-09", "active_hours": 3},
        ]
        assert current_streak() == 3

    @patch("src.metrics.daily_activity")
    def test_broken_streak(self, mock):
        mock.return_value = [
            {"date": "2026-04-07", "active_hours": 2},
            {"date": "2026-04-08", "active_hours": 0.3},  # below threshold
            {"date": "2026-04-09", "active_hours": 3},
        ]
        assert current_streak() == 1

    @patch("src.metrics.daily_activity")
    def test_skips_zero_filled_gap_days(self, mock):
        mock.return_value = [
            {"date": "2026-04-10", "active_hours": 2.5, "has_data": True},
            {"date": "2026-04-11", "active_hours": 0.0, "has_data": False},
            {"date": "2026-04-12", "active_hours": 3.0, "has_data": True},
        ]
        assert current_streak() == 2


# ── daily_activity ───────────────────────────────────────

class TestDailyActivity:
    @patch("src.metrics.samples_in_range", return_value=[])
    def test_no_data(self, mock):
        assert daily_activity(days=7) == []

    @patch("src.metrics.samples_in_range")
    def test_with_data(self, mock):
        now = int(time.time())
        mock.return_value = [
            (now - 120, 10, 10, 0, None, None, None, None, None, None),
            (now - 60, 15, 10, 0, None, None, None, None, None, None),
            (now, 20, 10, 0, None, None, None, None, None, None),
        ]
        result = daily_activity(days=1)
        assert len(result) >= 1

    @patch("src.metrics.samples_in_range")
    def test_fills_missing_days_with_zeroes(self, mock):
        day1 = int(time.mktime((2026, 4, 10, 9, 0, 0, 0, 0, -1)))
        day3 = int(time.mktime((2026, 4, 12, 9, 0, 0, 0, 0, -1)))
        mock.return_value = [
            (day1, 10, 10, 0, None, None, None, None, None, None),
            (day1 + 60, 15, 10, 0, None, None, None, None, None, None),
            (day3, 20, 10, 0, None, None, None, None, None, None),
            (day3 + 60, 25, 10, 0, None, None, None, None, None, None),
        ]
        with patch("src.metrics.time.time", return_value=day3 + 60):
            result = daily_activity(days=3)
        assert [row["date"] for row in result] == ["2026-04-10", "2026-04-11", "2026-04-12"]
        assert result[1]["active_hours"] == 0.0
        assert result[1]["has_data"] is False


# ── _compute_active_mask ─────────────────────────────────

class TestComputeActiveMask:
    def test_empty(self):
        assert _compute_active_mask([]) == []

    def test_no_changes(self):
        samples = [(100, 50, 20, 0), (200, 50, 20, 0), (300, 50, 20, 0)]
        mask = _compute_active_mask(samples)
        assert not any(mask)

    def test_with_changes(self):
        samples = [(100, 50, 20, 0), (200, 55, 20, 0), (300, 60, 20, 0)]
        mask = _compute_active_mask(samples)
        assert any(mask)
