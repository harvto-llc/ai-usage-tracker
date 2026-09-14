"""Tests for self-imposed quotas (plans.toml [<provider>.self_quota])."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

from src.plan_config import (
    CODEX_CREDIT_USD,
    model_cost_rates,
    model_credit_rates,
    self_quota_config,
)
from src import self_quota


@pytest.fixture(autouse=True)
def _fresh_cache():
    self_quota.invalidate_cache()
    yield
    self_quota.invalidate_cache()


# ── config parsing ───────────────────────────────────────

class TestSelfQuotaConfig:
    def test_absent_returns_none(self):
        assert self_quota_config("claude", {"claude": {"plan": "Max"}}) is None
        assert self_quota_config("claude", {}) is None

    def test_no_caps_returns_none(self):
        plans = {"claude": {"self_quota": {"window_hours": 5}}}
        assert self_quota_config("claude", plans) is None

    def test_token_caps_with_defaults(self):
        plans = {"claude": {"self_quota": {"session_cap_tokens": 1000}}}
        cfg = self_quota_config("claude", plans)
        assert cfg["session_cap_tokens"] == 1000
        assert cfg["window_hours"] == 5
        assert cfg["weekly_days"] == 7

    def test_cost_caps(self):
        plans = {"codex": {"self_quota": {"session_cap_usd": 10.0, "weekly_cap_usd": 75.0, "window_hours": 4}}}
        cfg = self_quota_config("codex", plans)
        assert cfg["session_cap_usd"] == 10.0
        assert cfg["window_hours"] == 4.0


class TestModelCostRates:
    def test_longest_prefix_wins(self):
        assert model_cost_rates("claude-fable-5")["output"] == 50.0
        assert model_cost_rates("claude-haiku-4-5")["output"] == 5.0

    def test_unknown_model_uses_default(self):
        assert model_cost_rates("mystery-model") == model_cost_rates("nope")

    def test_pricing_override(self):
        rates = model_cost_rates("claude-fable-5", {"claude-fable": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}})
        assert rates["output"] == 2

    def test_sonnet_5_promo_is_effective_dated(self):
        promo = model_cost_rates(
            "claude-sonnet-5",
            at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        )
        standard = model_cost_rates(
            "claude-sonnet-5",
            at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        assert promo["output"] == 10.0
        assert standard["output"] == 15.0

    @pytest.mark.parametrize(
        ("model", "input_rate", "output_rate"),
        [
            ("claude-fable-5", 10.0, 50.0),
            ("claude-opus-5", 5.0, 25.0),
            ("gpt-5.6-sol", 5.0, 30.0),
            ("gpt-5.6-terra", 2.5, 15.0),
            ("gpt-5.6-luna", 1.0, 6.0),
        ],
    )
    def test_current_model_rates(self, model, input_rate, output_rate):
        rates = model_cost_rates(model, at=datetime(2026, 7, 24, tzinfo=timezone.utc))

        assert rates["input"] == input_rate
        assert rates["output"] == output_rate

    def test_codex_credit_rates_and_dollar_conversion(self):
        rates = model_credit_rates("gpt-5.6-terra")

        assert rates == {"input": 62.5, "cache_read": 6.25, "output": 375.0}
        assert rates["input"] * CODEX_CREDIT_USD == 2.5
        assert model_credit_rates("gpt-5.3-codex-spark") is None

    @pytest.mark.parametrize(
        ("model", "input_rate", "output_rate"),
        [
            ("claude-mythos-5", 10.0, 50.0),
            ("claude-opus-4-7", 5.0, 25.0),
            ("claude-opus-4-5", 5.0, 25.0),
            ("claude-haiku-3-5", 0.8, 4.0),
        ],
    )
    def test_additional_published_claude_rates(self, model, input_rate, output_rate):
        rates = model_cost_rates(model, at=datetime(2026, 7, 24, tzinfo=timezone.utc))

        assert rates["input"] == input_rate
        assert rates["output"] == output_rate


# ── usage windows ────────────────────────────────────────

def _write_claude_jsonl(tmp_path, entries):
    proj = tmp_path / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    lines = []
    for ts, model, usage in entries:
        lines.append(json.dumps({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": {"role": "assistant", "model": model, "usage": usage},
        }))
    (proj / "s.jsonl").write_text("\n".join(lines) + "\n")


class TestClaudeWindows:
    def test_session_and_weekly_split(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_claude_jsonl(tmp_path, [
            # inside session window (1h ago): 100 in + 50 out
            (now - timedelta(hours=1), "claude-fable-5",
             {"input_tokens": 100, "output_tokens": 50}),
            # outside session, inside weekly (2 days ago): 1000 out
            (now - timedelta(days=2), "claude-sonnet-5",
             {"output_tokens": 1000}),
            # outside both (30 days ago): ignored
            (now - timedelta(days=30), "claude-sonnet-5",
             {"output_tokens": 999999}),
        ])
        with patch("pathlib.Path.home", return_value=tmp_path):
            totals = self_quota._claude_usage_windows(
                now - timedelta(hours=5), now - timedelta(days=7), None
            )
        assert totals["session"]["tokens"] == 150
        assert totals["weekly"]["tokens"] == 1150
        # cost: Fable 5 is $10 input / $50 output per Mtok.
        expected_session_cost = (100 * 10 + 50 * 50) / 1_000_000
        assert totals["session"]["cost_usd"] == pytest.approx(expected_session_cost)


class TestCodexWindows:
    def test_token_count_events(self, tmp_path):
        now = datetime.now(timezone.utc)
        root = tmp_path / "sessions" / "2026" / "07" / "03"
        root.mkdir(parents=True)
        turn_context = {
            "timestamp": (now - timedelta(hours=1, seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra"},
        }
        entry = {
            "timestamp": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 200, "output_tokens": 100,
                    "reasoning_output_tokens": 50, "cached_input_tokens": 25,
                }},
            },
        }
        (root / "s.jsonl").write_text(json.dumps(turn_context) + "\n" + json.dumps(entry) + "\n")
        totals = self_quota._codex_usage_windows(
            now - timedelta(hours=5), now - timedelta(days=7), None,
            sessions_root=tmp_path / "sessions",
        )
        # reasoning_output_tokens is a subset of output_tokens, not additive.
        assert totals["session"]["tokens"] == 300
        expected_cost = (175 * 2.5 + 100 * 15 + 25 * 0.25) / 1_000_000
        assert totals["session"]["cost_usd"] == pytest.approx(expected_cost)
        assert totals["weekly"]["tokens"] == 300


# ── snapshot ─────────────────────────────────────────────

class TestSnapshot:
    def test_unconfigured_returns_none(self):
        with patch("src.self_quota.self_quota_config", return_value=None):
            assert self_quota.self_quota_snapshot("claude") is None

    def test_cursor_self_quota_is_not_supported(self):
        cfg = {
            "window_hours": 5,
            "weekly_days": 7,
            "session_cap_tokens": 1000,
            "weekly_cap_tokens": None,
            "session_cap_usd": None,
            "weekly_cap_usd": None,
            "pricing": None,
            "models": {},
        }
        with patch("src.self_quota.self_quota_config", return_value=cfg):
            assert self_quota.self_quota_snapshot("cursor") is None

    def test_token_basis_pct(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_claude_jsonl(tmp_path, [
            (now - timedelta(hours=1), "claude-fable-5",
             {"input_tokens": 400, "output_tokens": 100}),
        ])
        cfg = {
            "window_hours": 5, "weekly_days": 7,
            "session_cap_tokens": 1000, "weekly_cap_tokens": 2000,
            "session_cap_usd": None, "weekly_cap_usd": None, "pricing": None,
        }
        with patch("src.self_quota.self_quota_config", return_value=cfg), \
             patch("pathlib.Path.home", return_value=tmp_path):
            snap = self_quota.self_quota_snapshot("claude")
        assert snap["session_used_pct"] == 50.0
        assert snap["weekly_used_pct"] == 25.0
        assert snap["session_remaining_pct"] == 50.0
        assert snap["source"] == "self_quota"
        assert snap["session_detail"] == {"basis": "tokens", "used": 500, "cap": 1000}

    def test_cost_basis_pct(self, tmp_path):
        now = datetime.now(timezone.utc)
        # 1M output tokens on sonnet = $15
        _write_claude_jsonl(tmp_path, [
            (now - timedelta(hours=1), "claude-sonnet-5",
             {"output_tokens": 1_000_000}),
        ])
        cfg = {
            "window_hours": 5, "weekly_days": 7,
            "session_cap_tokens": None, "weekly_cap_tokens": None,
            "session_cap_usd": 30.0, "weekly_cap_usd": 150.0,
            "pricing": {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}},
        }
        with patch("src.self_quota.self_quota_config", return_value=cfg), \
             patch("pathlib.Path.home", return_value=tmp_path):
            snap = self_quota.self_quota_snapshot("claude")
        assert snap["session_used_pct"] == 50.0
        assert snap["weekly_used_pct"] == 10.0
        assert snap["session_detail"]["basis"] == "cost"

    def test_over_cap_exceeds_100(self, tmp_path):
        now = datetime.now(timezone.utc)
        _write_claude_jsonl(tmp_path, [
            (now - timedelta(hours=1), "claude-fable-5",
             {"input_tokens": 3000, "output_tokens": 0}),
        ])
        cfg = {
            "window_hours": 5, "weekly_days": 7,
            "session_cap_tokens": 1000, "weekly_cap_tokens": None,
            "session_cap_usd": None, "weekly_cap_usd": None, "pricing": None,
        }
        with patch("src.self_quota.self_quota_config", return_value=cfg), \
             patch("pathlib.Path.home", return_value=tmp_path):
            snap = self_quota.self_quota_snapshot("claude")
        assert snap["session_used_pct"] == 300.0
        assert snap["session_remaining_pct"] == 0.0


# ── API overlay ──────────────────────────────────────────

class TestOverlay:
    SNAP = {
        "source": "self_quota",
        "session_used_pct": 42.0, "weekly_used_pct": 21.0,
        "session_remaining_pct": 58.0, "weekly_remaining_pct": 79.0,
        "session_reset": None, "weekly_reset": None,
        "window_hours": 5.0, "weekly_days": 7.0,
        "session_detail": {"basis": "tokens", "used": 42, "cap": 100},
        "weekly_detail": {"basis": "tokens", "used": 21, "cap": 100},
        "models": {"claude-fable": {"session_used_pct": 10.0}},
    }

    def test_fills_missing_quota(self):
        from src.api import _overlay_self_quota
        quota = {"session_used_pct": None, "weekly_used_pct": None}
        with patch("src.self_quota.self_quota_snapshot", return_value=self.SNAP):
            _overlay_self_quota(quota, "claude")
        assert quota["session_used_pct"] == 42.0
        assert quota["source"] == "self_quota"
        assert quota["self_quota"]["session"]["cap"] == 100
        assert quota["self_quota"]["models"]["claude-fable"]["session_used_pct"] == 10.0

    def test_does_not_override_live_scrape(self):
        from src.api import _overlay_self_quota
        quota = {"session_used_pct": 83.0, "weekly_used_pct": 12.0}
        with patch("src.self_quota.self_quota_snapshot", return_value=self.SNAP):
            _overlay_self_quota(quota, "claude", stale=False)
        assert quota["session_used_pct"] == 83.0
        assert "source" not in quota

    def test_overrides_stale_scrape(self):
        from src.api import _overlay_self_quota
        quota = {"session_used_pct": 83.0, "weekly_used_pct": 12.0}
        with patch("src.self_quota.self_quota_snapshot", return_value=self.SNAP):
            _overlay_self_quota(quota, "claude", stale=True)
        assert quota["session_used_pct"] == 42.0
        assert quota["source"] == "self_quota"
