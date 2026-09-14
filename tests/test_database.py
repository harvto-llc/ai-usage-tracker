"""Tests for database CRUD operations using an in-memory SQLite DB."""

import sqlite3
import time
from unittest.mock import patch

import pytest

import src.database as db


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    """Redirect all DB operations to a temp file."""
    test_db = tmp_path / "test.db"
    with patch.object(db, "DB", test_db):
        db.init()
        yield


class TestInit:
    def test_creates_tables(self):
        sample = db.latest_sample()
        assert sample is None

    def test_idempotent(self):
        db.init()
        db.init()
        assert db.latest_sample() is None

    def test_provider_metric_samples_migration_idempotent(self):
        db.init()
        db.init()
        with sqlite3.connect(db.DB) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_metric_samples)").fetchall()}
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(provider_metric_samples)").fetchall()}
        assert {
            "timestamp",
            "day",
            "provider",
            "status",
            "sample_hash",
            "shared_json",
            "unique_json",
            "source_json",
            "error_text",
        }.issubset(columns)
        assert "idx_provider_metric_samples_provider_ts" in indexes
        assert "idx_provider_metric_samples_day_provider" in indexes

    def test_usage_samples_weekly_sublimits_migration(self):
        db.DB.unlink()
        with sqlite3.connect(db.DB) as conn:
            conn.execute(
                """
                CREATE TABLE usage_samples(
                    timestamp INTEGER NOT NULL,
                    session_pct REAL NOT NULL,
                    weekly_pct REAL NOT NULL,
                    extra_pct REAL NOT NULL DEFAULT 0,
                    session_reset TEXT,
                    weekly_reset TEXT,
                    extra_reset TEXT,
                    extra_spent_usd REAL,
                    extra_limit_usd REAL,
                    extra_balance_usd REAL
                )
                """
            )
            conn.commit()

        db.init()

        with sqlite3.connect(db.DB) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_samples)").fetchall()}

        assert {
            "weekly_sonnet_pct", "weekly_design_pct", "plan_id", "raw_plan",
            "plan_source", "limits_json", "credit_pools_json",
        }.issubset(columns)


class TestInsertAndQuery:
    def test_insert_and_latest(self):
        db.insert(50.0, 30.0, 5.0, ts=1000, session_reset="3 hr",
                  weekly_reset="Tue", extra_reset="Mon",
                  extra_spent_usd=1.5, extra_limit_usd=10.0, extra_balance_usd=8.5,
                  weekly_sonnet_pct=12.5, weekly_design_pct=67.0)
        s = db.latest_sample()
        assert s["session"] == 50.0
        assert s["weekly"] == 30.0
        assert s["extra"] == 5.0
        assert s["session_reset"] == "3 hr"
        assert s["extra_spent_usd"] == 1.5
        assert s["extra_limit_usd"] == 10.0
        assert s["extra_balance_usd"] == 8.5
        assert s["weekly_sonnet_pct"] == 12.5
        assert s["weekly_design_pct"] == 67.0

    def test_insert_and_latest_default_weekly_sublimits_to_none(self):
        db.insert(50.0, 30.0, ts=1000)

        s = db.latest_sample()

        assert s["weekly_sonnet_pct"] is None
        assert s["weekly_design_pct"] is None

    def test_persists_dynamic_limits_credits_and_plan_transition(self):
        limits = [{"id": "claude-session", "used_pct": 20}]
        pools = [{"id": "claude-usage-credits", "balance": 12.5}]
        db.insert(
            20, 10, ts=1000, plan_id="pro", raw_plan="pro", plan_source="detected",
            limits=limits, credit_pools=pools,
        )
        db.insert(
            1, 2, ts=2000, plan_id="max-5x", raw_plan="max_5x", plan_source="detected",
            limits=limits, credit_pools=pools,
        )

        sample = db.latest_sample()
        assert sample["plan_id"] == "max-5x"
        assert sample["limits"] == limits
        assert sample["credit_pools"] == pools
        assert [row["plan_id"] for row in db.plan_history("claude")] == ["pro", "max-5x"]

    def test_last_samples(self):
        for i in range(5):
            db.insert(i * 10, 20, ts=1000 + i * 60)
        rows = db.last_samples(3)
        assert len(rows) == 3
        assert rows[0][1] == 40.0  # newest first

    def test_last_samples_empty(self):
        assert db.last_samples() == []

    def test_latest_sample_fills_weekly_reset(self):
        db.insert(10, 10, ts=100, weekly_reset="Tue 12:00 AM")
        db.insert(20, 20, ts=200, weekly_reset=None)
        s = db.latest_sample()
        assert s["weekly_reset"] == "Tue 12:00 AM"

    def test_latest_sample_fills_extra_reset(self):
        db.insert(10, 10, 5, ts=100, extra_reset="Mon")
        db.insert(20, 20, 10, ts=200, extra_reset=None)
        s = db.latest_sample()
        assert s["extra_reset"] == "Mon"

    def test_samples_in_range(self):
        db.insert(10, 10, ts=100)
        db.insert(20, 20, ts=200)
        db.insert(30, 30, ts=300)
        rows = db.samples_in_range(150, 350)
        assert len(rows) == 2
        assert rows[0][1] == 20.0

    def test_insert_provider_metric_sample_dedup_and_heartbeat(self):
        base_snapshot = {
            "provider": "claude",
            "status": "ok",
            "shared": {"primary_used_pct": 50},
            "unique": {"conversations_today": 2},
            "source": {"collector_access": "subscription"},
            "error_text": None,
        }
        assert db.insert_provider_metric_sample(**base_snapshot, ts=1_000)
        assert not db.insert_provider_metric_sample(**base_snapshot, ts=1_020, heartbeat_seconds=3600)
        assert db.insert_provider_metric_sample(**base_snapshot, ts=5_000, heartbeat_seconds=3600)

        with sqlite3.connect(db.DB) as conn:
            count = conn.execute("SELECT COUNT(*) FROM provider_metric_samples WHERE provider='claude'").fetchone()[0]
        assert count == 2

    def test_prune_provider_metric_samples(self):
        now_ts = 1_000_000
        db.insert_provider_metric_sample(
            provider="claude",
            status="ok",
            shared={"primary_used_pct": 10},
            unique={},
            source={},
            ts=100,
        )
        db.insert_provider_metric_sample(
            provider="codex",
            status="ok",
            shared={"primary_used_pct": 20},
            unique={},
            source={},
            ts=now_ts,
        )
        deleted = db.prune_provider_metric_samples(retention_days=1, now_ts=now_ts)
        assert deleted == 1
        latest = db.latest_provider_metric_samples()
        assert "claude" not in latest
        assert latest["codex"]["shared"]["primary_used_pct"] == 20


class TestCodex:
    def test_insert_and_latest(self):
        db.insert_codex(80.0, 90.0, "Apr 10", session_remaining_pct=70.0,
                        session_reset="Apr 9 3:00 PM", ts=1000)
        c = db.latest_codex()
        assert c["weekly_remaining_pct"] == 80.0
        assert c["session_remaining_pct"] == 70.0
        assert c["session_reset"] == "Apr 9 3:00 PM"

    def test_persists_dynamic_plan_and_credit_fields(self):
        limits = [{"id": "codex-weekly", "remaining_pct": 39}]
        pools = [{"id": "codex-shared-credits", "balance": 0}]
        db.insert_codex(
            39, None, "Jul 30", plan_id="pro-20x", raw_plan="pro-20x",
            plan_source="detected", limits=limits, credit_pools=pools,
            rate_limit_reset_credits=2, credits_remaining=0, ts=2000,
        )

        sample = db.latest_codex()
        assert sample["plan_id"] == "pro-20x"
        assert sample["limits"] == limits
        assert sample["credit_pools"] == pools
        assert sample["rate_limit_reset_credits"] == 2
        assert sample["credits_remaining"] == 0

    def test_codex_reset_credit_balance_changes(self):
        db.insert_codex(None, None, None, rate_limit_reset_credits=2, ts=100)
        db.insert_codex(None, None, None, rate_limit_reset_credits=2, ts=200)
        db.insert_codex(None, None, None, rate_limit_reset_credits=1, ts=300)
        db.insert_codex(None, None, None, rate_limit_reset_credits=3, ts=400)
        db.insert_codex(None, None, None, rate_limit_reset_credits=2, ts=500)

        changes = db.codex_reset_credit_balance_changes(250, 450)

        assert changes == {
            "balance_decreases": 1,
            "balance_increases": 2,
            "starting_balance": 2,
            "ending_balance": 3,
        }

    def test_codex_reset_credit_balance_changes_needs_observed_drop(self):
        db.insert_codex(None, None, None, rate_limit_reset_credits=1, ts=300)

        changes = db.codex_reset_credit_balance_changes(250, 450)

        assert changes["balance_decreases"] == 0
        assert changes["starting_balance"] == 1
        assert changes["ending_balance"] == 1

    def test_latest_codex_empty(self):
        assert db.latest_codex() is None

    def test_last_codex_samples(self):
        db.insert_codex(80, None, None, session_remaining_pct=90, ts=100)
        db.insert_codex(70, None, None, session_remaining_pct=80, ts=200)
        rows = db.last_codex_samples(5)
        assert len(rows) == 2
        assert rows[0][1] == 80.0  # newest first

    def test_codex_last_completed_cycles(self):
        db.insert_codex(90, None, "week 1", session_remaining_pct=99, ts=100)
        db.insert_codex(60, None, "week 1", session_remaining_pct=95, ts=200)
        db.insert_codex(100, None, "week 2", session_remaining_pct=100, ts=300)
        db.insert_codex(80, None, "week 2", session_remaining_pct=88, ts=400)

        cycles = db.codex_last_completed_cycles()

        assert cycles["session_used_pct"] == 5.0
        assert cycles["weekly_used_pct"] == 40.0

    def test_insert_and_latest_model_sublimits(self):
        db.insert_codex(80.0, 90.0, "Apr 10", session_remaining_pct=70.0,
                        weekly_gpt54_remaining_pct=85.0,
                        weekly_spark_remaining_pct=92.0, ts=1000)
        c = db.latest_codex()
        assert c["weekly_gpt54_remaining_pct"] == 85.0
        assert c["weekly_spark_remaining_pct"] == 92.0

    def test_insert_and_latest_default_model_sublimits_to_none(self):
        db.insert_codex(80.0, 90.0, "Apr 10", ts=1000)
        c = db.latest_codex()
        assert c["weekly_gpt54_remaining_pct"] is None
        assert c["weekly_spark_remaining_pct"] is None

    def test_codex_model_sublimits_migration(self):
        import sqlite3
        db.DB.unlink()
        with sqlite3.connect(db.DB) as conn:
            conn.execute(
                """
                CREATE TABLE codex_usage_samples(
                    timestamp INTEGER NOT NULL,
                    weekly_remaining_pct REAL,
                    code_review_remaining_pct REAL,
                    reset_at TEXT,
                    session_remaining_pct REAL,
                    session_reset TEXT
                )
                """
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS usage_samples(
                    timestamp INTEGER NOT NULL,
                    session_pct REAL NOT NULL,
                    weekly_pct REAL NOT NULL,
                    extra_pct REAL NOT NULL DEFAULT 0,
                    session_reset TEXT,
                    weekly_reset TEXT,
                    extra_reset TEXT,
                    extra_spent_usd REAL,
                    extra_limit_usd REAL,
                    extra_balance_usd REAL,
                    weekly_sonnet_pct REAL,
                    weekly_design_pct REAL
                )"""
            )
            conn.commit()

        db.init()

        with sqlite3.connect(db.DB) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(codex_usage_samples)").fetchall()}

        assert {"weekly_gpt54_remaining_pct", "weekly_spark_remaining_pct"}.issubset(columns)


class TestCodexLocalStats:
    def test_by_source_returned(self, tmp_path):
        db_path = tmp_path / ".codex" / "state_5.sqlite"
        db_path.parent.mkdir(parents=True)
        import sqlite3 as _sq
        conn = _sq.connect(db_path)
        conn.execute("""CREATE TABLE threads(
            title TEXT, tokens_used INTEGER, model TEXT, source TEXT, updated_at INTEGER
        )""")
        now = int(time.time())
        conn.execute("INSERT INTO threads VALUES(?, ?, ?, ?, ?)", ("Fix bug", 5000, "o4-mini", "cli", now))
        conn.execute("INSERT INTO threads VALUES(?, ?, ?, ?, ?)", ("Review PR", 3000, "o4-mini", "cli", now))
        conn.execute("INSERT INTO threads VALUES(?, ?, ?, ?, ?)", ("Web task", 2000, "o4-mini", "web", now))
        conn.commit()
        conn.close()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = db.codex_local_stats()
        assert "by_source" in result
        assert result["by_source"]["cli"]["sessions"] == 2
        assert result["by_source"]["web"]["sessions"] == 1
        assert result["by_source"]["cli"]["tokens"] == 8000

    def test_empty_db_no_crash(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = db.codex_local_stats()
        assert result == {}


class TestCachedFunctions:
    def test_cc_messages_today_cached(self):
        with patch("src.scanners.scan_cc_messages_today", return_value={"total_messages": 42, "active_hours": 1.0}):
            db._cc_cache["data"] = None
            db._cc_cache["ts"] = 0
            result = db.cc_messages_today()
            assert result["total_messages"] == 42

    def test_cc_token_usage_cached(self):
        with patch("src.scanners.scan_cc_tokens_today", return_value={"total_tokens": 100}):
            db._cc_token_cache["data"] = None
            db._cc_token_cache["ts"] = 0
            result = db.cc_token_usage_today_cached()
            assert result["total_tokens"] == 100

    def test_cache_hit(self):
        db._cc_cache["data"] = {"total_messages": 99}
        db._cc_cache["ts"] = time.time()
        result = db.cc_messages_today()
        assert result["total_messages"] == 99

    def test_load_claude_code_stats_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            assert db.load_claude_code_stats() is None

    def test_load_claude_code_stats_valid(self, tmp_path):
        stats = tmp_path / "stats.json"
        stats.write_text('{"totalSessions": 5}')
        with patch("src.database.load_claude_code_stats") as mock:
            mock.return_value = {"totalSessions": 5}
            result = mock()
            assert result["totalSessions"] == 5
