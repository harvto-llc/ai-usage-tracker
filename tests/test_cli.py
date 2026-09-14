import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cli import (
    CLIError,
    fetch_stats,
    fetch_explanations,
    history_data,
    load_api_secret,
    render_cost,
    render_usage,
    render_explanations,
)


def sample_stats():
    return {
        "claude_quota": {
            "plan_label": "Max 5x",
            "limits": [
                {
                    "id": "claude-weekly",
                    "label": "Weekly quota",
                    "used_pct": 39,
                    "remaining_pct": 61,
                    "reset": "Jul 30 5:31 PM",
                }
            ],
        },
        "codex_quota": {
            "plan_label": "Pro",
            "credit_pools": [
                {
                    "id": "codex-shared-credits",
                    "label": "Shared credits",
                    "balance": 12,
                    "unit": "credits",
                    "kind": "purchased",
                }
            ],
            "limits": [
                {
                    "id": "codex-session",
                    "label": "5 hour quota",
                    "used_pct": 20,
                    "remaining_pct": 80,
                    "reset": "10:00 PM",
                }
            ],
        },
        "provider_health": {
            "claude": {"quota": {"status": "current"}},
            "codex": {"quota": {"status": "current"}},
        },
        "provider_periods": {
            "claude": {"day": {"total_tokens": 1_250_000, "requests": 12, "estimated_cost_usd": 4.5, "estimated_credits": None, "pricing_coverage_pct": 100}},
            "codex": {"day": {"total_tokens": 2_500_000, "requests": 20, "estimated_cost_usd": 2.0, "estimated_credits": 50, "pricing_coverage_pct": 80}},
        },
        "provider_trends": {
            "claude": {"points": [{"date": f"2026-07-{day:02d}", "total_tokens": day} for day in range(1, 31)]},
            "codex": {"points": []},
        },
    }


def test_load_api_secret_prefers_environment(tmp_path):
    config = tmp_path / "config"
    config.write_text("USAGE_TRACKER_SECRET=file-secret\n")
    with patch.dict("os.environ", {"USAGE_TRACKER_SECRET": "env-secret"}, clear=False):
        assert load_api_secret(config) == "env-secret"


def test_load_api_secret_reads_config(tmp_path):
    config = tmp_path / "config"
    config.write_text("USAGE_TRACKER_SECRET=file-secret\n")
    with patch.dict("os.environ", {"USAGE_TRACKER_SECRET": ""}, clear=False):
        assert load_api_secret(config) == "file-secret"


def test_fetch_stats_sends_bearer_token_without_returning_it():
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def opener(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response(json.dumps({"ok": True}).encode())

    assert fetch_stats("http://localhost:8000/", "private", opener=opener) == {"ok": True}
    assert captured == {"authorization": "Bearer private", "timeout": 15}


def test_fetch_explanations_requests_each_provider_without_exposing_token():
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def opener(request, timeout):
        requests.append((request.full_url, request.get_header("Authorization"), timeout))
        return Response(json.dumps({"provider": request.full_url.split("provider=")[1].split("&")[0]}).encode())

    data = fetch_explanations(
        "http://localhost:8000",
        "private",
        "all",
        "week",
        opener=opener,
    )

    assert set(data) == {"claude", "codex"}
    assert all(auth == "Bearer private" and timeout == 15 for _, auth, timeout in requests)
    assert all("period=week" in url for url, _, _ in requests)


def test_usage_output_preserves_dynamic_limit_labels():
    output = render_usage(sample_stats(), "all")
    assert "Weekly quota" in output
    assert "5 hour quota" in output
    assert "Max 5x" in output
    assert "61%" in output
    assert "Reported balances" in output
    assert "Shared credits" in output


def test_cost_output_labels_estimates_not_spend():
    output = render_cost(sample_stats(), "all", "day")
    assert "Est. value" in output
    assert "Est. credits" in output
    assert "$4.50" in output
    assert "Spend" not in output


def test_explanation_output_marks_local_estimate_and_drivers():
    output = render_explanations({
        "claude": {
            "totals": {"total_tokens": 1_000, "effective_tokens": 500},
            "coverage": {"classified_pct": 99.5},
            "categories": [{
                "id": "files", "label": "Files", "share_pct": 60,
                "total_tokens": 600, "effective_tokens": 300,
                "estimated_cost_usd": 0.25, "estimated_credits": None,
            }],
        }
    })

    assert "Files" in output
    assert "60%" in output
    assert "local estimate, not quota debit" in output


def test_history_returns_only_requested_trailing_days():
    points = history_data(sample_stats(), "claude", 7)["claude"]
    assert len(points) == 7
    assert points[0]["date"] == "2026-07-24"


def test_missing_token_has_actionable_error(tmp_path):
    with patch.dict("os.environ", {"USAGE_TRACKER_SECRET": ""}, clear=False):
        with pytest.raises(CLIError, match="config"):
            load_api_secret(Path(tmp_path / "missing"))
