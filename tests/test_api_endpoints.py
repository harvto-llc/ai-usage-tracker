"""Tests for API endpoints using FastAPI TestClient."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

import src.database as db
import src.api as api_module
from src.api import API_SECRET, _remote_cc, app

AUTH = {"Authorization": f"Bearer {API_SECRET}"}
EXPECTED_PROVIDERS = {"claude", "codex", "cursor"}


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    test_db = tmp_path / "test.db"
    with patch.object(db, "DB", test_db):
        db.init()
        yield


@pytest.fixture(autouse=True)
def reset_remote_cc():
    for key in list(_remote_cc.keys()):
        _remote_cc[key] = 0 if key == "ts" else None
    api_module._stats_cache = None
    api_module._stats_cache_ts = 0
    yield
    for key in list(_remote_cc.keys()):
        _remote_cc[key] = 0 if key == "ts" else None
    api_module._stats_cache = None
    api_module._stats_cache_ts = 0


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded_db():
    """Insert sample data for endpoints that need it."""
    db.insert(50, 30, 5, ts=int(time.time()) - 60,
              session_reset="3 hr", weekly_reset="Tue 12:00 AM",
              extra_spent_usd=1.5, extra_limit_usd=10.0, extra_balance_usd=8.5)


class TestAuth:
    def test_no_token_returns_401(self, client):
        r = client.get("/stats")
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, client):
        r = client.get("/stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_valid_token_passes(self, client, seeded_db):
        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200


class TestSecurityHeaders:
    def test_headers_present(self, client, seeded_db):
        r = client.get("/stats", headers=AUTH)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"


class TestHealth:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestUsageExplanation:
    def test_requires_authentication(self, client):
        assert client.get("/usage/explanation?provider=claude&period=day").status_code == 401

    def test_returns_period_breakdown(self, client):
        payload = {
            "provider": "claude",
            "period": "day",
            "window_start_at": "2026-07-25T00:00:00+00:00",
            "window_end_at": "2026-07-25T12:00:00+00:00",
            "categories": [],
        }
        with patch.object(
            api_module.usage_explanation,
            "build_usage_explanation",
            return_value=payload,
        ) as build:
            response = client.get(
                "/usage/explanation?provider=claude&period=day&start_at=2026-07-25T00%3A00%3A00%2B00%3A00",
                headers=AUTH,
            )

        assert response.status_code == 200
        assert response.json() == payload
        assert build.call_args.args == ("claude", "day")
        assert build.call_args.kwargs["start_at"].tzinfo is not None

    def test_rejects_invalid_provider_or_period(self, client):
        assert client.get(
            "/usage/explanation?provider=cursor&period=day", headers=AUTH
        ).status_code == 400
        assert client.get(
            "/usage/explanation?provider=claude&period=month", headers=AUTH
        ).status_code == 400


class TestStatsEndpoint:
    def test_concurrent_cold_requests_share_one_cache_fill(self):
        started = threading.Event()
        release = threading.Event()
        calls = []
        payload = {"shared": True}

        def slow_build():
            calls.append(1)
            started.set()
            assert release.wait(timeout=2)
            api_module._stats_cache = payload
            api_module._stats_cache_ts = time.time()
            return payload

        with patch.object(api_module, "_build_stats", side_effect=slow_build):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(api_module.stats)
                assert started.wait(timeout=1)
                second = pool.submit(api_module.stats)
                time.sleep(0.05)
                assert calls == [1]
                release.set()
                assert first.result(timeout=1) == payload
                assert second.result(timeout=1) == payload

        assert calls == [1]

    def test_empty_db(self, client):
        # No scraped samples: /stats still returns a full payload with null
        # quota gauges (self-imposed quotas / API-based access rely on
        # this; the Swift app requires the non-optional `extra` field).
        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["extra"] == 0.0
        assert data["claude_quota"]["session_used_pct"] is None
        assert data["timestamp"] is None

    def test_with_data(self, client, seeded_db):
        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "burn" in data
        assert "risk_outlook" in data
        assert data["claude_today"]["messages_today"] is not None
        assert data["claude_quota"]["session_used_pct"] == 50
        assert data["claude_quota"]["weekly_sonnet_used_pct"] is None
        assert data["claude_quota"]["weekly_design_used_pct"] is None
        registry_ids = {item["id"] for item in data["provider_registry"]}
        assert registry_ids == EXPECTED_PROVIDERS
        assert set(data["providers_latest"].keys()) == EXPECTED_PROVIDERS
        assert set(data["provider_periods"].keys()) == {"claude", "codex"}
        assert set(data["provider_trends"].keys()) == {"claude", "codex"}
        assert set(data["provider_health"].keys()) == {"claude", "codex"}
        assert data["pricing_catalog"]["source"] in {"bundled", "local_cache"}
        assert data["pricing_catalog"]["codex_credit_usd_estimate"]["official"] is False
        assert data["pricing_catalog"]["codex_credit_usd_estimate"]["checkout_verified"] is True
        assert data["provider_periods"]["claude"]["day"]["messages"] is not None
        assert data["providers_latest"]["claude"]["status"] in {"stale", "partial", "ok", "error"}
        claude_quota_health = data["provider_health"]["claude"]["quota"]
        assert claude_quota_health["status"] == "current"
        assert claude_quota_health["source"] == "subscription usage"
        assert claude_quota_health["age_seconds"] >= 0

    def test_feed_health_distinguishes_stale_and_unavailable(self):
        stale = api_module._feed_health(
            available=True,
            source="subscription usage",
            timestamp=1_000,
            now=2_000,
            stale_after_seconds=660,
            stale_recovery="Refresh sign-in",
        )
        unavailable = api_module._feed_health(
            available=False,
            source="Codex analytics",
            timestamp=None,
            now=2_000,
            unavailable_detail="Not reported for this plan",
        )

        assert stale == {
            "status": "stale",
            "source": "subscription usage",
            "timestamp": 1_000,
            "age_seconds": 1_000,
            "detail": "Last successful refresh was 16m ago",
            "recovery": "Refresh sign-in",
        }
        assert unavailable["status"] == "unavailable"
        assert unavailable["detail"] == "Not reported for this plan"
        assert unavailable["recovery"] is None

    def test_claude_past_time_only_session_reset_is_unknown(self, client):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 7, 5, 14, 29)
                return value.replace(tzinfo=tz) if tz else value

        db.insert(
            87,
            59,
            0,
            ts=int(time.time()) - 60,
            session_reset="2:20pm",
            weekly_reset="Jul 6 at 5:59pm",
        )

        with patch.object(api_module, "datetime", FixedDateTime):
            r = client.get("/stats", headers=AUTH)

        assert r.status_code == 200
        data = r.json()
        assert data["cc_session_hours_left"] is None
        assert data["claude_quota"]["session_reset"] is None
        assert data["claude_quota"]["weekly_reset"] == "Jul 6 5:59 PM"

    def test_stats_exposes_normalized_codex_contract(self, client, seeded_db):
        db.insert_codex(80, 25, "Apr 10", session_remaining_pct=90, session_reset="Apr 9 3:00 PM", ts=1000)
        _remote_cc["messages"] = {"total_messages": 4, "active_hours": 1.5, "conversations": 2}
        _remote_cc["tokens"] = {"input_tokens": 200, "output_tokens": 80, "cache_read_tokens": 0, "cache_create_tokens": 0}
        _remote_cc["codex_local"] = {
            "total_tokens": 900,
            "total_sessions": 3,
            "recent_threads": [],
            "today_tokens": 400,
            "today_threads": 1,
            "by_model": {"o4-mini": {"tokens": 900, "sessions": 3}},
            "today_by_model": {"o4-mini": {"tokens": 400, "sessions": 1}},
        }
        _remote_cc["codex_sessions"] = {
            "messages_today": 2,
            "sessions_today": 1,
            "total_sessions": 7,
            "active_hours_today": 0.5,
            "input_tokens_today": 120,
            "output_tokens_today": 60,
            "user_messages_today": 1,
            "reasoning_tokens_today": 30,
        }
        _remote_cc["cc_stats"] = {"total_sessions": 12, "total_messages": 120, "daily_activity": []}
        _remote_cc["ts"] = time.time()

        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["claude_today"]["messages_today"] == 4
        assert data["claude_today"]["output_tokens_today"] == 80
        assert data["claude_today"]["threads_today"] is None
        assert data["claude_today"]["sessions_today"] is None
        assert data["codex_today"]["messages_today"] == 2
        assert data["codex_today"]["threads_today"] == 1
        assert data["codex_today"]["sessions_today"] == 1
        for legacy_key in (
            "active_hours",
            "messages",
            "sessions",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "user_messages",
        ):
            assert legacy_key not in data["codex_today"]
        assert "codex" not in data
        assert data["codex_totals"]["total_threads"] == 3
        assert data["codex_totals"]["total_sessions"] == 7
        assert data["claude_quota"]["session_used_pct"] == 50
        assert data["codex_quota"]["weekly_used_pct"] == 20
        assert data["codex_quota"]["session_used_pct"] == 10
        assert data["codex_quota"]["weekly_reset"] == "Apr 10"

        _remote_cc["messages"] = None
        _remote_cc["tokens"] = None
        _remote_cc["codex_local"] = None
        _remote_cc["codex_sessions"] = None
        _remote_cc["cc_stats"] = None
        _remote_cc["ts"] = 0

    def test_stats_exposes_codex_model_sublimits(self, client, seeded_db):
        db.insert_codex(80, 25, "Apr 10", session_remaining_pct=90,
                        weekly_gpt54_remaining_pct=85.0,
                        weekly_spark_remaining_pct=92.0, ts=1000)
        _remote_cc["ts"] = time.time()

        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        codex_quota = r.json()["codex_quota"]
        assert codex_quota["weekly_gpt54_used_pct"] == 15.0
        assert codex_quota["weekly_spark_used_pct"] == 8.0

        _remote_cc["ts"] = 0

    def test_stats_accepts_legacy_codex_session_aliases_for_transition(self, client, seeded_db):
        _remote_cc["codex_local"] = {
            "total_tokens": 500,
            "total_sessions": 2,
            "today_tokens": 100,
            "today_threads": 1,
            "recent_threads": [],
            "by_model": {},
            "today_by_model": {},
        }
        _remote_cc["codex_sessions"] = {
            "messages": 3,
            "sessions": 1,
            "active_hours": 0.4,
            "input_tokens": 50,
            "output_tokens": 20,
            "reasoning_tokens": 10,
            "user_messages": 2,
            "total_sessions": 6,
        }
        _remote_cc["ts"] = time.time()

        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["codex_today"]["messages_today"] == 3
        assert data["codex_today"]["sessions_today"] == 1
        assert data["codex_today"]["active_hours_today"] == 0.4
        assert data["codex_today"]["input_tokens_today"] == 50
        assert data["codex_today"]["output_tokens_today"] == 20
        assert data["codex_today"]["reasoning_tokens_today"] == 10
        assert data["codex_today"]["user_messages_today"] == 2
        assert "messages" not in data["codex_today"]
        assert "sessions" not in data["codex_today"]

        _remote_cc["codex_local"] = None
        _remote_cc["codex_sessions"] = None
        _remote_cc["ts"] = 0

    def test_codex_analytics_summary_present(self, client, seeded_db):
        """codex_analytics_summary is present in /stats response."""
        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "codex_analytics_summary" in data

    def test_stats_exposes_gap_rollups(self, client, seeded_db):
        """/stats exposes 4-state gap rollups for today/yesterday/last_7d."""
        from datetime import datetime

        import src.gap_rollups as gap_rollups_module

        today_key = datetime.now(gap_rollups_module._local_timezone()).strftime("%Y-%m-%d")

        synthetic = {
            "period_days": 7,
            "daily": [
                {
                    "date": today_key,
                    "focus_gap_sec": 1800,
                    "attention_idle_sec": 600,
                    "off_hours_away_sec": 0,
                    "agent_runtime_sec": 1200,
                },
                {
                    "date": "1900-01-01",  # synthetic "yesterday-or-earlier" row
                    "focus_gap_sec": 300,
                    "attention_idle_sec": 100,
                    "off_hours_away_sec": 7200,
                    "agent_runtime_sec": 400,
                },
            ],
        }

        with patch.object(gap_rollups_module, "session_analytics", return_value=synthetic):
            r = client.get("/stats", headers=AUTH)

        assert r.status_code == 200
        rollups = r.json()["gap_rollups"]
        assert rollups is not None
        for bucket in ("today", "yesterday", "last_7d"):
            assert bucket in rollups
            for key in (
                "focus_gap_sec",
                "attention_idle_sec",
                "off_hours_away_sec",
                "agent_runtime_sec",
                "human_time_sec",
                "downtime_sec",
            ):
                assert key in rollups[bucket]

        # Today row picked up by today_key match
        today = rollups["today"]
        assert today["focus_gap_sec"] == 1800
        assert today["attention_idle_sec"] == 600
        assert today["agent_runtime_sec"] == 1200
        # Legacy fields: human_time = focus + attention; downtime zeroed.
        assert today["human_time_sec"] == 2400
        assert today["downtime_sec"] == 0

        # last_7d sums every daily row in the period.
        last_7d = rollups["last_7d"]
        assert last_7d["focus_gap_sec"] == 1800 + 300
        assert last_7d["off_hours_away_sec"] == 7200
        assert last_7d["agent_runtime_sec"] == 1600
        assert last_7d["downtime_sec"] == 0

    def test_stale_remote_quota_data_is_hidden(self, client, seeded_db):
        _remote_cc["cursor_usage"] = {"plan": "Pro", "total_requests": 42, "total_tokens": 9000}
        _remote_cc["ts"] = time.time() - 600
        r = client.get("/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["cursor"] is None
        _remote_cc["cursor_usage"] = None
        _remote_cc["ts"] = 0

    def test_codex_analytics_summary_none(self, client, seeded_db):
        with patch("src.api.codex_local_stats", return_value={}):
            r = client.get("/stats", headers=AUTH)
            data = r.json()
            assert data["codex_analytics_summary"] is None

    def test_codex_analytics_summary_with_data(self, client, seeded_db):
        _remote_cc["codex_local"] = {
            "total_tokens": 8000,
            "total_sessions": 5,
            "by_model": {},
            "by_source": {
                "cli": {"tokens": 5000, "sessions": 3},
                "web": {"tokens": 2000, "sessions": 1},
                "ide": {"tokens": 1000, "sessions": 1},
            },
            "recent_threads": [],
        }
        _remote_cc["messages"] = {"total_messages": 1, "active_hours": 0}
        _remote_cc["tokens"] = {"total_tokens": 0}
        _remote_cc["ts"] = time.time()
        r = client.get("/stats", headers=AUTH)
        data = r.json()
        ca = data["codex_analytics_summary"]
        assert ca is not None
        assert ca["dominant_surface"] == "cli"
        assert ca["dominant_share_pct"] == 60
        assert ca["total_threads"] == 5
        _remote_cc["ts"] = 0
        _remote_cc["codex_local"] = None
        _remote_cc["messages"] = None
        _remote_cc["tokens"] = None

    def test_codex_analytics_summary_prefers_normalized_period_surfaces(self):
        period = {
            "surfaces": {
                "loop": {"total_tokens": 9000, "sessions": 2},
                "cli": {"total_tokens": 1000, "sessions": 1},
            }
        }
        with patch("src.api.codex_local_stats", return_value={
            "by_source": {"vscode": {"tokens": 10000, "sessions": 3}}
        }):
            summary = api_module._codex_analytics_summary(period)

        assert summary["dominant_surface"] == "loop"
        assert summary["dominant_share_pct"] == 90
        assert summary["surfaces"] == {"loop": 2, "cli": 1}


class TestCCReport:
    def test_post_report(self, client):
        payload = {
            "messages": {"total_messages": 10, "active_hours": 1},
            "tokens": {"total_tokens": 500},
            "projects": [{"project": "test", "active_hours": 1, "messages": 5}],
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_post_without_auth(self, client):
        payload = {"messages": {}, "tokens": {}, "projects": []}
        r = client.post("/cc/report", json=payload)
        assert r.status_code == 401

    def test_post_with_usage_inserts_to_db(self, client):
        payload = {
            "messages": {"total_messages": 5},
            "tokens": {"total_tokens": 100},
            "projects": [],
            "usage": {
                "session_pct": 40,
                "weekly_pct": 20,
                "extra_pct": 0,
                "weekly_sonnet_pct": 12.5,
                "weekly_design_pct": 67.0,
            },
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        s = db.latest_sample()
        assert s is not None
        assert s["session"] == 40
        assert s["weekly_sonnet_pct"] == 12.5
        assert s["weekly_design_pct"] == 67.0

        stats = client.get("/stats", headers=AUTH)
        assert stats.status_code == 200
        claude_quota = stats.json()["claude_quota"]
        assert claude_quota["weekly_sonnet_used_pct"] == 12.5
        assert claude_quota["weekly_design_used_pct"] == 67.0

    def test_post_with_codex_usage(self, client):
        payload = {
            "messages": {}, "tokens": {}, "projects": [],
            "codex_usage": {
                "weekly_remaining_pct": 80,
                "code_review_remaining_pct": 70,
                "session_remaining_pct": 90,
                "weekly_reset": "Apr 10",
                "session_reset": "Apr 9 3:00 PM",
            },
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        c = db.latest_codex()
        assert c is not None
        assert c["reset_at"] == "Apr 10"
        assert c["code_review_remaining_pct"] == 70
        assert c["session_reset"] == "Apr 9 3:00 PM"

    def test_post_with_codex_model_sublimits(self, client):
        payload = {
            "messages": {}, "tokens": {}, "projects": [],
            "codex_usage": {
                "weekly_remaining_pct": 80,
                "code_review_remaining_pct": 70,
                "session_remaining_pct": 90,
                "weekly_reset": "Apr 10",
                "session_reset": "Apr 9 3:00 PM",
                "weekly_gpt54_remaining_pct": 85,
                "weekly_spark_remaining_pct": 92,
            },
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        c = db.latest_codex()
        assert c is not None
        assert c["weekly_gpt54_remaining_pct"] == 85
        assert c["weekly_spark_remaining_pct"] == 92

    def test_post_exposes_dynamic_codex_plan_account_limit_and_credits(self, client):
        monthly = {
            "id": "codex-codex-primary",
            "label": "Monthly",
            "window_kind": "monthly",
            "window_minutes": 43200,
            "scope_kind": "aggregate",
            "used_pct": 84,
            "remaining_pct": 16,
            "reset": "Aug 4 7:23 PM",
        }
        credits = [{
            "id": "codex-shared-credits",
            "label": "Shared credits",
            "kind": "purchased",
            "unit": "credits",
            "balance": 0,
        }]
        payload = {
            "messages": {}, "tokens": {}, "projects": [],
            "codex_usage": {
                "raw_plan": "free",
                "limits": [monthly],
                "credit_pools": credits,
                "credits_remaining": 0,
            },
        }

        assert client.post("/cc/report", json=payload, headers=AUTH).status_code == 200
        quota = client.get("/stats", headers=AUTH).json()["codex_quota"]

        assert quota["plan_id"] == "free"
        assert quota["plan_label"] == "Free"
        assert quota["plan_entitlements"]["monthly_usd"] == 0
        assert quota["session_used_pct"] is None
        assert quota["account_used_pct"] == 84
        assert quota["account_reset"] == "Aug 4 7:23 PM"
        assert quota["credits_remaining"] == 0
        assert quota["limits"] == [monthly]

    def test_stats_exposes_codex_banked_reset_usage_by_period(self, client):
        now = int(time.time())
        db.insert_codex(
            None,
            None,
            None,
            credit_pools=[{
                "id": "codex-banked-resets",
                "label": "Banked resets",
                "kind": "reset",
                "unit": "resets",
                "balance": 2,
            }],
            rate_limit_reset_credits=2,
            ts=now - 120,
        )
        db.insert_codex(
            None,
            None,
            None,
            credit_pools=[{
                "id": "codex-banked-resets",
                "label": "Banked resets",
                "kind": "reset",
                "unit": "resets",
                "balance": 1,
            }],
            rate_limit_reset_credits=1,
            ts=now - 60,
        )

        quota = client.get("/stats", headers=AUTH).json()["codex_quota"]

        assert quota["rate_limit_reset_credits"] == 1
        assert quota["rate_limit_reset_credits_used_day"] == 1
        assert quota["rate_limit_reset_credits_used_week"] == 1
        assert quota["rate_limit_reset_credits_granted_day"] == 0
        assert quota["rate_limit_reset_credits_granted_week"] == 0

    def test_post_exposes_claude_max_plan_dynamic_limits_and_credit_semantics(self, client):
        limits = [{
            "id": "claude-weekly-sonnet",
            "label": "Sonnet weekly",
            "window_kind": "weekly",
            "scope_kind": "model",
            "model": "claude-sonnet",
            "used_pct": 11,
            "remaining_pct": 89,
            "reset": "Jul 27 6:00 PM",
        }]
        pools = [{
            "id": "claude-usage-credits",
            "label": "Usage credits",
            "kind": "prepaid",
            "unit": "usd",
            "balance": None,
            "spent_month": 12,
            "monthly_spend_cap": 50,
            "monthly_spend_headroom": 38,
        }]
        payload = {
            "messages": {}, "tokens": {}, "projects": [],
            "usage": {
                "session_pct": 7,
                "weekly_pct": 18,
                "raw_plan": "max_5x",
                "limits": limits,
                "credit_pools": pools,
            },
        }

        assert client.post("/cc/report", json=payload, headers=AUTH).status_code == 200
        data = client.get("/stats", headers=AUTH).json()
        quota = data["claude_quota"]

        assert quota["plan_id"] == "max-5x"
        assert quota["plan_label"] == "Max 5x"
        assert quota["plan_entitlements"]["session_capacity_vs_pro"] == 5
        assert quota["limits"] == limits
        assert quota["credit_pools"] == pools
        assert data["plan_history"]["claude"][0]["plan_id"] == "max-5x"

    def test_internal_codex_pro_plan_is_normalized_in_stats(self, client):
        payload = {
            "messages": {}, "tokens": {}, "projects": [],
            "codex_usage": {"plan_id": "prolite"},
        }

        assert client.post("/cc/report", json=payload, headers=AUTH).status_code == 200
        quota = client.get("/stats", headers=AUTH).json()["codex_quota"]

        assert quota["plan_id"] == "pro"
        assert quota["plan_label"] == "Pro"
        assert quota["plan_entitlements"]["credit_topup"] is True
        assert quota["plan_entitlements"]["expected_spark_access"] is True

    def test_post_with_codex_analytics(self, client):
        payload = {
            "messages": {},
            "tokens": {},
            "projects": [],
            "codex_analytics": {
                "window_days": 30,
                "summary": {
                    "workspace": {"avg_daily_users": 12.0},
                    "sessions_messages": {"avg_daily_user_messages": 20.0, "avg_daily_credits": 3.5},
                    "code_review": {"avg_daily_reviews": 4.0, "avg_daily_comments": 8.0},
                },
                "daily_sessions_messages_counts": {
                    "data": [{"credit_total": 2.5}, {"credit_total": 3.25}],
                },
                "credit_usage_events": {
                    "data": [{"credit_amount": 2.5}, {"credit_amount": 3.25}],
                },
                "daily_code_review_metrics": {"data": [{"n_reviews": 4}]},
            },
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        assert _remote_cc["codex_analytics"]["summary"]["workspace"]["avg_daily_users"] == 12.0
        summary = client.get("/stats", headers=AUTH).json()["codex_analytics_summary"]
        assert summary["credits_used"] == 5.75
        assert summary["credits_used_usd"] == 0.23
        assert summary["credits_used_usd_is_estimate"] is False
        assert summary["credit_events_count"] == 2
        assert summary["avg_daily_credits"] == 3.5
        assert summary["credits_window_days"] == 30

    def test_post_without_codex_analytics_does_not_clear_cached_bundle(self, client):
        _remote_cc["codex_analytics"] = {"summary": {"workspace": {"avg_daily_users": 12.0}}}
        payload = {"messages": {}, "tokens": {}, "projects": []}
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        assert _remote_cc["codex_analytics"]["summary"]["workspace"]["avg_daily_users"] == 12.0

    def test_fast_report_does_not_clear_cached_quota_payloads(self, client):
        _remote_cc["usage"] = {"session_pct": 12, "plan_id": "max-5x"}
        _remote_cc["codex_usage"] = {"weekly_remaining_pct": 19, "plan_id": "pro"}

        payload = {"messages": {}, "tokens": {}, "projects": []}
        assert client.post("/cc/report", json=payload, headers=AUTH).status_code == 200

        assert _remote_cc["usage"]["session_pct"] == 12
        assert _remote_cc["codex_usage"]["weekly_remaining_pct"] == 19

    def test_post_provider_snapshots_persists_latest_samples(self, client):
        payload = {
            "messages": {"total_messages": 1, "active_hours": 0.5},
            "tokens": {"total_tokens": 10},
            "projects": [],
            "provider_snapshots": {
                "claude": {
                    "provider": "claude",
                    "timestamp": int(time.time()),
                    "status": "ok",
                    "shared": {"primary_used_pct": 11.0},
                    "unique": {"conversations_today": 2},
                    "source": {"collector_access": "subscription"},
                    "error_text": None,
                }
            },
        }
        r = client.post("/cc/report", json=payload, headers=AUTH)
        assert r.status_code == 200
        latest = db.latest_provider_metric_samples()
        assert latest["claude"]["status"] == "ok"
        assert latest["claude"]["shared"]["primary_used_pct"] == 11.0


class TestSentinelAPI:
    def test_startup_migrates_managed_cookie_files(self, tmp_path):
        base_dir = tmp_path / ".usage-tracker"
        base_dir.mkdir()
        (base_dir / "claude-cookie.txt").write_text("claude-session-key")
        (base_dir / "codex-cookie.txt").write_text("codex-session-token")
        env_file = tmp_path / ".env"
        env_file.write_text(
            f'export CLAUDE_WEB_COOKIE_FILE="{base_dir / "claude-cookie.txt"}"\n'
            f'export CODEX_WEB_COOKIE_FILE="{base_dir / "codex-cookie.txt"}"\n'
        )
        stored = {}

        def store(provider, value):
            stored[provider] = value
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(api_module, "_sentinel_env_file", return_value=env_file),
            patch.object(api_module, "keychain_enabled", return_value=True),
            patch.object(api_module, "load_provider_credential", side_effect=lambda provider: stored.get(provider)),
            patch.object(api_module, "store_provider_credential", side_effect=store),
        ):
            migrated = api_module._migrate_managed_provider_credentials()

        assert migrated == {"claude": "keychain", "codex": "keychain"}
        assert stored == {"claude": "claude-session-key", "codex": "codex-session-token"}
        assert not (base_dir / "claude-cookie.txt").exists()
        assert not (base_dir / "codex-cookie.txt").exists()
        assert "WEB_COOKIE_FILE" not in env_file.read_text()

    def test_report_cookies_uses_keychain_for_claude_and_codex(self, client, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            'export CLAUDE_WEB_COOKIE_FILE="old-claude"\n'
            'export CODEX_WEB_COOKIE_FILE="old-codex"\n'
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(api_module, "_sentinel_env_file", return_value=env_file),
            patch.object(api_module, "store_provider_credential", return_value=True) as store,
        ):
            payload = {
                "cookies": {
                    "claude": "sessionKey=claude-session-key; lastActiveOrg=org-123",
                    "codex": "__Secure-next-auth.session-token=codex-session-token",
                    "cursor": "WorkosCursorSessionToken=cursor-token"
                }
            }
            r = client.post("/sentinel/report", json=payload, headers=AUTH)
            assert r.status_code == 200
            assert "claude" in r.json()["received"]
            assert r.json()["storage"] == {
                "claude": "keychain",
                "codex": "keychain",
                "cursor": "file",
            }

            base_dir = tmp_path / ".usage-tracker"
            assert not (base_dir / "claude-cookie.txt").exists()
            assert not (base_dir / "codex-cookie.txt").exists()
            assert (base_dir / "cursor-cookie.txt").read_text() == "WorkosCursorSessionToken=cursor-token"
            assert "CLAUDE_WEB_COOKIE_FILE" not in env_file.read_text()
            assert "CODEX_WEB_COOKIE_FILE" not in env_file.read_text()
            assert store.call_count == 2

    def test_report_cookies_falls_back_to_restricted_files(self, client, tmp_path):
        env_file = tmp_path / ".env"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(api_module, "_sentinel_env_file", return_value=env_file),
            patch.object(api_module, "store_provider_credential", return_value=False),
        ):
            r = client.post(
                "/sentinel/report",
                json={"cookies": {
                    "claude": "sessionKey=claude-session-key; lastActiveOrg=org-123",
                    "codex": "__Secure-next-auth.session-token=codex-token",
                }},
                headers=AUTH,
            )

        assert r.status_code == 200
        assert r.json()["storage"] == {"claude": "file", "codex": "file"}
        base_dir = tmp_path / ".usage-tracker"
        for provider in ("claude", "codex"):
            cookie_file = base_dir / f"{provider}-cookie.txt"
            assert cookie_file.stat().st_mode & 0o777 == 0o600
            assert str(cookie_file) in env_file.read_text()

    def test_report_rejects_incomplete_claude_cookie(self, client, tmp_path):
        env_file = tmp_path / ".env"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch.object(api_module, "_sentinel_env_file", return_value=env_file),
            patch.object(api_module, "store_provider_credential") as store,
        ):
            r = client.post(
                "/sentinel/report",
                json={"cookies": {"claude": "ARID=incomplete"}},
                headers=AUTH,
            )

        assert r.status_code == 200
        assert r.json()["received"] == []
        assert r.json()["rejected"] == ["claude"]
        store.assert_not_called()


class TestBudgetWeekly:
    def test_requires_auth(self, client):
        r = client.get("/budget/weekly")
        assert r.status_code == 401

    def test_returns_forecasts_dict(self, client, seeded_db):
        r = client.get("/budget/weekly", headers=AUTH)
        assert r.status_code == 200
        assert "forecasts" in r.json()


class TestModelsToday:
    def test_claude_models_today_sums_all_token_kinds(self):
        from src.api import _claude_today_contract

        cc_tokens = {
            "output_tokens": 10,
            "input_tokens": 5,
            "cache_read_tokens": 100,
            "cache_create_tokens": 20,
            "by_model": {
                "claude-sonnet-4-5": {
                    "input": 5, "output": 10,
                    "cache_read": 100, "cache_create": 20,
                    "requests": 3,
                },
                "bogus": "not-a-dict",
            },
        }
        result = _claude_today_contract({}, cc_tokens)
        assert result["models_today"] == {
            "claude-sonnet-4-5": {"tokens": 135, "requests": 3}
        }

    def test_claude_models_today_empty_when_absent(self):
        from src.api import _claude_today_contract

        assert _claude_today_contract({}, {})["models_today"] == {}

    def test_codex_models_today_from_today_by_model(self):
        from src.api import _codex_today_contract

        local = {
            "today_by_model": {
                "gpt-5.2-codex": {"tokens": 42000, "threads": 4},
                "bogus": None,
            }
        }
        result = _codex_today_contract(local, {})
        assert result["models_today"] == {
            "gpt-5.2-codex": {"tokens": 42000, "requests": 4}
        }
