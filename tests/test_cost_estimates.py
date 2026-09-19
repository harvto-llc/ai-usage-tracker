import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src import cost_estimates
from src.plan_config import CODEX_CREDIT_USD


@pytest.fixture(autouse=True)
def _fresh_cache():
    cost_estimates.invalidate_cache()
    yield
    cost_estimates.invalidate_cache()


def _limit(now: datetime, *, model: str | None = None) -> dict:
    return {
        "id": "window",
        "label": "Session",
        "window_kind": "session",
        "window_minutes": 300,
        "scope_kind": "model" if model else "aggregate",
        "model": model,
        "reset_at": int((now + timedelta(hours=1)).timestamp()),
    }


def test_claude_estimate_prices_cache_write_ttls(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    entry = {
        "timestamp": (now - timedelta(hours=1)).isoformat(),
        "message": {
            "id": "msg_one",
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 15,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 5,
                },
            },
        },
    }
    (root / "s.jsonl").write_text(json.dumps(entry) + "\n" + json.dumps(entry) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        estimate = cost_estimates.quota_cost_estimates("claude", [_limit(now)], now=now)[0]

    expected = (100 * 10 + 50 * 50 + 20 * 1 + 10 * 12.5 + 5 * 20) / 1_000_000
    assert estimate["estimated_cost_usd"] == pytest.approx(expected)
    assert estimate["observed_tokens"] == 185
    assert estimate["pricing_coverage_pct"] == 100.0
    assert estimate["estimated_credits"] is None
    assert estimate["pricing_rates"][0]["source_label"] == "Anthropic Claude API pricing"


def test_codex_estimate_uses_turn_model_and_does_not_double_count_reasoning(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "24"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(hours=1, seconds=1)).isoformat(),
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra"},
        },
        {
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 200,
                    "cached_input_tokens": 25,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 50,
                }},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        estimate = cost_estimates.quota_cost_estimates("codex", [_limit(now)], now=now)[0]

    expected_credits = (175 * 62.5 + 25 * 6.25 + 100 * 375) / 1_000_000
    assert estimate["observed_tokens"] == 300
    assert estimate["estimated_credits"] == pytest.approx(expected_credits, abs=0.0005)
    assert estimate["estimated_cost_usd"] == pytest.approx(
        expected_credits * CODEX_CREDIT_USD, abs=0.00005
    )
    assert estimate["pricing_coverage_pct"] == 100.0
    assert estimate["pricing_rates"][0]["effective_from"].startswith("2026-04-02")


def test_codex_event_before_token_rate_migration_is_unpriced(tmp_path):
    now = datetime(2026, 4, 1, 20, tzinfo=timezone.utc)
    root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "01"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(minutes=10, seconds=1)).isoformat(),
            "payload": {"type": "turn_context", "model": "gpt-5.6-terra"},
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 10}},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        estimate = cost_estimates.quota_cost_estimates("codex", [_limit(now)], now=now)[0]

    assert estimate["estimated_cost_usd"] is None
    assert estimate["estimated_credits"] is None
    assert estimate["pricing_coverage_pct"] == 0.0
    assert estimate["unpriced_reasons"] == {
        "gpt-5.6-terra": "No published rate for this model and event date."
    }


def test_unpublished_spark_rate_is_reported_as_unpriced(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "24"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(minutes=10, seconds=1)).isoformat(),
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex-spark"},
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 10}},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        estimate = cost_estimates.quota_cost_estimates(
            "codex", [_limit(now, model="gpt-5.3-codex-spark")], now=now
        )[0]

    assert estimate["estimated_cost_usd"] is None
    assert estimate["estimated_credits"] is None
    assert estimate["pricing_coverage_pct"] == 0.0
    assert estimate["unpriced_models"] == ["gpt-5.3-codex-spark"]


def test_claude_period_metrics_follow_day_and_week_boundaries(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "message": {
                "id": "weekly",
                "role": "assistant",
                "model": "claude-opus-4-5",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        },
        {
            "timestamp": (now - timedelta(hours=1, minutes=5)).isoformat(),
            "message": {"role": "user", "content": "question"},
        },
        {
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "message": {
                "id": "daily",
                "role": "assistant",
                "model": "claude-fable-5",
                "usage": {"input_tokens": 200, "output_tokens": 80},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    windows = {
        "day": (now - timedelta(hours=2), now),
        "week": (now - timedelta(days=7), now),
    }

    with patch("pathlib.Path.home", return_value=tmp_path):
        metrics = cost_estimates.observed_period_metrics("claude", windows, now=now)

    assert metrics["day"]["total_tokens"] == 280
    assert metrics["day"]["messages"] == 2
    assert metrics["day"]["sessions"] == 1
    assert metrics["day"]["models"]["claude-fable-5"] == {"tokens": 280, "requests": 1}
    assert metrics["week"]["total_tokens"] == 430
    assert metrics["week"]["messages"] == 3
    assert metrics["week"]["estimated_cost_usd"] > metrics["day"]["estimated_cost_usd"]


def test_codex_period_metrics_include_credits_and_reasoning_without_double_counting(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".codex" / "sessions" / "2026" / "07" / "24"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(minutes=10, seconds=2)).isoformat(),
            "payload": {"type": "turn_context", "model": "gpt-5.6-terra"},
        },
        {
            "timestamp": (now - timedelta(minutes=10, seconds=1)).isoformat(),
            "payload": {"type": "user_message", "role": "user", "content": "question"},
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 200,
                    "cached_input_tokens": 25,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 50,
                }},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    windows = {"day": (now - timedelta(hours=1), now)}

    with patch("pathlib.Path.home", return_value=tmp_path):
        metrics = cost_estimates.observed_period_metrics("codex", windows, now=now)["day"]

    assert metrics["messages"] == 1
    assert metrics["user_messages"] == 1
    assert metrics["total_tokens"] == 300
    assert metrics["reasoning_tokens"] == 50
    assert metrics["estimated_credits"] > 0
    assert metrics["estimated_cost_usd"] == pytest.approx(
        metrics["estimated_credits"] * CODEX_CREDIT_USD,
        abs=0.00005,
    )


def test_codex_loop_spark_usage_is_attributed_and_left_unpriced(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".loop" / "runs" / "project" / "32" / "codex-home" / "sessions" / "2026" / "07" / "25"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(seconds=3)).isoformat(),
            "type": "session_meta",
            "payload": {"id": "loop-spark", "originator": "loop", "source": "vscode"},
        },
        {
            "timestamp": (now - timedelta(seconds=2)).isoformat(),
            "payload": {"type": "turn_context", "model": "gpt-5.3-codex-spark"},
        },
        {
            "timestamp": (now - timedelta(seconds=1)).isoformat(),
            "payload": {"type": "user_message", "role": "user", "content": "question"},
        },
        {
            "timestamp": now.isoformat(),
            "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 800,
                "output_tokens": 50,
            }}},
        },
    ]
    (root / "spark.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        metrics = cost_estimates.observed_period_metrics(
            "codex", {"day": (now - timedelta(hours=1), now)}, now=now
        )["day"]

    assert metrics["models"]["gpt-5.3-codex-spark"] == {"tokens": 1050, "requests": 1}
    assert metrics["surfaces"]["loop"]["total_tokens"] == 1050
    assert metrics["surfaces"]["loop"]["requests"] == 1
    assert metrics["surfaces"]["loop"]["sessions"] == 1
    assert metrics["estimated_cost_usd"] is None
    assert metrics["estimated_credits"] is None
    assert metrics["pricing_coverage_pct"] == 0.0
    assert metrics["unpriced_models"] == ["gpt-5.3-codex-spark"]


def test_usage_bundle_scans_once_for_quota_and_periods(tmp_path):
    now = datetime.now(timezone.utc)
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    row = {
        "timestamp": (now - timedelta(minutes=10)).isoformat(),
        "message": {
            "id": "combined",
            "role": "assistant",
            "model": "claude-fable-5",
            "usage": {"input_tokens": 200, "output_tokens": 80},
        },
    }
    (root / "s.jsonl").write_text(json.dumps(row) + "\n")
    windows = {
        "day": (now - timedelta(hours=2), now),
        "week": (now - timedelta(days=7), now),
    }

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(cost_estimates, "_scan_claude", wraps=cost_estimates._scan_claude) as scan,
    ):
        bundle = cost_estimates.usage_bundle("claude", [_limit(now)], windows, now=now)

    assert scan.call_count == 1
    assert bundle["quota_estimates"][0]["observed_tokens"] == 280
    assert bundle["periods"]["day"]["total_tokens"] == 280
    assert bundle["periods"]["week"]["total_tokens"] == 280


def test_provider_usage_trend_zero_fills_and_reports_top_driver(tmp_path, local_timezone):
    local_timezone("UTC")  # the fixture's timestamps and day buckets are written in UTC
    now = datetime(2026, 7, 24, 20, tzinfo=timezone.utc)
    root = tmp_path / ".claude" / "projects" / "p"
    root.mkdir(parents=True)
    rows = [
        {
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "message": {
                "id": "trend-one",
                "role": "assistant",
                "model": "claude-fable-5",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        },
        {
            "timestamp": now.isoformat(),
            "message": {
                "id": "trend-two",
                "role": "assistant",
                "model": "claude-fable-5",
                "usage": {"input_tokens": 200, "output_tokens": 80},
            },
        },
    ]
    (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    with patch("pathlib.Path.home", return_value=tmp_path):
        trend = cost_estimates.provider_usage_trend("claude", days=7, now=now)

    assert len(trend["points"]) == 7
    assert trend["points"][-3]["total_tokens"] == 0
    assert trend["points"][-2]["total_tokens"] == 150
    assert trend["points"][-1]["total_tokens"] == 280
    assert trend["top_model_7d"]["model"] == "claude-fable-5"
    assert trend["top_model_7d"]["label"] == "Fable 5"
    assert "Fable 5 drove 100%" in trend["advisor"]
    assert trend["points"][-1]["pricing_coverage_pct"] == 100.0


def test_cached_bundle_returns_immediately_and_deduplicates_refresh(tmp_path):
    now = datetime.now(timezone.utc)
    windows = {"day": (now - timedelta(hours=2), now)}
    started = threading.Event()
    release = threading.Event()
    result = {
        "quota_estimates": [],
        "periods": {"day": {"total_tokens": 1}},
        "status": "current",
        "updated_at": int(time.time()),
    }

    def slow_bundle(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2)
        return result

    cost_estimates.invalidate_cache()
    with (
        patch.dict(os.environ, {"PYTEST_CURRENT_TEST": ""}),
        patch.object(cost_estimates, "_bundle_cache_path", return_value=tmp_path / "cache.json"),
        patch.object(cost_estimates, "usage_bundle", side_effect=slow_bundle) as scan,
    ):
        first = cost_estimates.cached_usage_bundle("claude", [_limit(now)], windows, now=now)
        assert started.wait(timeout=1)
        second = cost_estimates.cached_usage_bundle("claude", [_limit(now)], windows, now=now)
        release.set()
        deadline = time.time() + 2
        while "claude" in cost_estimates._bundle_pending and time.time() < deadline:
            time.sleep(0.01)

    assert first["status"] == "refreshing"
    assert second["status"] == "refreshing"
    assert scan.call_count == 1
    assert "claude" not in cost_estimates._bundle_pending
