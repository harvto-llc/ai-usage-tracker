"""Non-blocking orchestration for opt-in work-ledger refreshes."""

from __future__ import annotations

import os
import threading
import time

from src import (
    git_activity,
    session_runtime,
    session_search_index,
    usage_ledger,
    work_activity,
    work_ledger,
)


_refresh_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_scheduler_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def _capture(results: dict, key: str, operation) -> bool:
    try:
        results[key] = operation()
        return True
    except Exception as exc:
        results[key] = {"status": "error", "error": str(exc)}
        return False


def refresh_once(*, now: int | None = None) -> dict:
    timestamp = int(now if now is not None else time.time())
    if not work_ledger.settings()["collection_enabled"]:
        return work_ledger.update_refresh_state(
            "paused",
            finished_at=timestamp,
            result={},
            now=timestamp,
        )

    work_ledger.update_refresh_state(
        "running",
        started_at=timestamp,
        finished_at=None,
        result={},
        now=timestamp,
    )
    results: dict[str, dict] = {}
    healthy = True
    healthy = _capture(
        results,
        "claude_runtime",
        lambda: session_runtime.sync_live_claude_sessions(now=timestamp),
    ) and healthy
    for provider in ("claude", "codex"):
        usage_ok = _capture(
            results,
            f"{provider}_sessions",
            lambda provider=provider: usage_ledger.sync_provider(provider),
        )
        healthy = usage_ok and healthy
        if usage_ok:
            healthy = _capture(
                results,
                f"{provider}_activity",
                lambda provider=provider: work_activity.sync_provider_activity(
                    provider,
                    sync_sessions=True,
                ),
            ) and healthy
    healthy = _capture(
        results,
        "session_search",
        session_search_index.refresh,
    ) and healthy
    healthy = _capture(
        results,
        "git",
        lambda: git_activity.index_configured_repositories(
            session_cwds=work_ledger.session_cwds(),
            now=timestamp,
        ),
    ) and healthy
    status = "ok" if healthy else "partial"
    errors = [
        f"{key}: {value['error']}"
        for key, value in results.items()
        if value.get("status") == "error"
    ]
    return work_ledger.update_refresh_state(
        status,
        finished_at=int(time.time()),
        error="; ".join(errors) or None,
        result=results,
    )


def _background_refresh() -> None:
    try:
        refresh_once()
    finally:
        global _refresh_thread
        with _refresh_lock:
            _refresh_thread = None


def start_background_refresh() -> dict:
    global _refresh_thread
    if not work_ledger.settings()["collection_enabled"]:
        return {"accepted": False, "reason": "collection_paused", "refresh": work_ledger.refresh_state()}
    with _refresh_lock:
        if _refresh_thread and _refresh_thread.is_alive():
            return {"accepted": False, "reason": "already_running", "refresh": work_ledger.refresh_state()}
        _refresh_thread = threading.Thread(
            target=_background_refresh,
            name="usage-tracker-work-refresh",
            daemon=True,
        )
        _refresh_thread.start()
    return {"accepted": True, "reason": "started", "refresh": work_ledger.refresh_state()}


def _scheduled_refreshes(interval_seconds: float) -> None:
    start_background_refresh()
    while not _scheduler_stop.wait(interval_seconds):
        start_background_refresh()


def start_refresh_scheduler(interval_seconds: float | None = None) -> dict:
    """Start one process-local scheduler for periodic, non-blocking refreshes."""
    global _scheduler_thread
    if interval_seconds is None:
        interval_seconds = float(
            os.environ.get("USAGE_TRACKER_WORK_REFRESH_INTERVAL", "60")
        )
    if interval_seconds <= 0:
        raise ValueError("refresh interval must be positive")
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return {"accepted": False, "reason": "already_running"}
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduled_refreshes,
            args=(interval_seconds,),
            name="usage-tracker-work-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()
    return {"accepted": True, "reason": "started"}


def stop_refresh_scheduler(timeout_seconds: float = 2) -> None:
    global _scheduler_thread
    with _scheduler_lock:
        thread = _scheduler_thread
        _scheduler_stop.set()
    if thread:
        thread.join(timeout=timeout_seconds)
    with _scheduler_lock:
        if _scheduler_thread is thread and (thread is None or not thread.is_alive()):
            _scheduler_thread = None
