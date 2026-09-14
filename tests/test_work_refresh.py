import threading
from unittest.mock import patch

import pytest

from src import session_search_index, usage_ledger, work_ledger, work_refresh


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    search_db_path = tmp_path / "session-search.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    monkeypatch.setenv("USAGE_TRACKER_SESSION_SEARCH_DB", str(search_db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    session_search_index._initialized_paths.discard(str(search_db_path))
    work_refresh._refresh_thread = None
    monkeypatch.setattr(
        work_refresh.session_runtime,
        "sync_live_claude_sessions",
        lambda **kwargs: {"provider": "claude", "status": "ok", "sessions": 0},
    )
    work_refresh.stop_refresh_scheduler()
    work_refresh._scheduler_thread = None
    yield
    work_refresh.stop_refresh_scheduler()


def test_refresh_is_paused_until_collection_is_enabled():
    result = work_refresh.refresh_once(now=10)

    assert result["status"] == "paused"
    assert result["finished_at"] == 10
    assert result["result"] == {}


def test_refresh_isolates_provider_failures_and_continues_git():
    work_ledger.configure_collection(enabled=True, now=1)

    def sync_provider(provider):
        if provider == "claude":
            raise RuntimeError("Claude history unavailable")
        return {"provider": provider, "events_inserted": 2}

    with patch.object(work_refresh.usage_ledger, "sync_provider", side_effect=sync_provider), patch.object(
        work_refresh.work_activity,
        "sync_provider_activity",
        return_value={"status": "ok", "events_inserted": 3},
    ) as activity, patch.object(
        work_refresh.session_search_index,
        "refresh",
        return_value={"status": "ok", "sessions_changed": 0},
    ), patch.object(
        work_refresh.git_activity,
        "index_configured_repositories",
        return_value={"status": "ok", "repositories": 1},
    ) as git:
        result = work_refresh.refresh_once(now=10)

    assert result["status"] == "partial"
    assert result["result"]["claude_sessions"]["status"] == "error"
    assert "Claude history unavailable" in result["last_error"]
    assert result["result"]["codex_sessions"]["events_inserted"] == 2
    activity.assert_called_once_with("codex", sync_sessions=True)
    git.assert_called_once()


def test_refresh_runs_each_source_when_healthy():
    work_ledger.configure_collection(enabled=True, now=1)
    with patch.object(
        work_refresh.usage_ledger,
        "sync_provider",
        side_effect=lambda provider: {"provider": provider, "events_inserted": 1},
    ), patch.object(
        work_refresh.work_activity,
        "sync_provider_activity",
        side_effect=lambda provider, sync_sessions: {
            "provider": provider,
            "status": "ok",
            "events_inserted": 1,
        },
    ), patch.object(
        work_refresh.session_search_index,
        "refresh",
        return_value={"status": "ok", "sessions_changed": 0},
    ), patch.object(
        work_refresh.git_activity,
        "index_configured_repositories",
        return_value={"status": "ok", "repositories": 0},
    ):
        result = work_refresh.refresh_once(now=10)

    assert result["status"] == "ok"
    assert set(result["result"]) == {
        "claude_runtime",
        "claude_sessions",
        "claude_activity",
        "codex_sessions",
        "codex_activity",
        "session_search",
        "git",
    }
    assert result["last_error"] is None


def test_scheduler_starts_immediately_and_repeats_without_overlapping_workers():
    repeated = threading.Event()
    calls = []

    def start_refresh():
        calls.append(1)
        if len(calls) >= 2:
            repeated.set()
        return {"accepted": True}

    with patch.object(work_refresh, "start_background_refresh", side_effect=start_refresh):
        started = work_refresh.start_refresh_scheduler(interval_seconds=0.01)
        duplicate = work_refresh.start_refresh_scheduler(interval_seconds=0.01)
        assert repeated.wait(timeout=1)
        work_refresh.stop_refresh_scheduler()

    assert started == {"accepted": True, "reason": "started"}
    assert duplicate == {"accepted": False, "reason": "already_running"}
    assert len(calls) >= 2


def test_scheduler_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="positive"):
        work_refresh.start_refresh_scheduler(interval_seconds=0)
