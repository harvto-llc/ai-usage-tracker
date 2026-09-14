"""Shared provider metric shaping helpers.

Centralizes the metric semantics used by the collector, fallback readers, and API.
Trimmed to the three tracked providers: claude, codex, cursor.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.entitlements import resolve_plan
from src.plan_config import load_plans
from src.scanners import _active_hours_from_timestamps

CODEX_LEGACY_LOCAL_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "total_threads": ("total_sessions",),
    "today_threads": ("today_sessions",),
}

CODEX_LEGACY_SESSION_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "active_hours_today": ("active_hours",),
    "messages_today": ("messages",),
    "sessions_today": ("sessions",),
    "input_tokens_today": ("input_tokens",),
    "output_tokens_today": ("output_tokens",),
    "reasoning_tokens_today": ("reasoning_tokens",),
    "cached_tokens_today": ("cached_tokens",),
    "user_messages_today": ("user_messages",),
}

PROVIDER_REGISTRY: tuple[dict[str, object], ...] = (
    {"id": "claude", "label": "Claude", "color": "#6dd3ff", "order": 1},
    {"id": "codex", "label": "Codex", "color": "#a78bfa", "order": 2},
    {"id": "cursor", "label": "Cursor", "color": "#fbbf24", "order": 3},
)

PROVIDER_IDS: tuple[str, ...] = tuple(str(item["id"]) for item in PROVIDER_REGISTRY)
PROVIDER_SNAPSHOT_STATUSES: tuple[str, ...] = ("ok", "partial", "error", "stale", "limit")

SNAPSHOT_SHARED_FIELDS: tuple[str, ...] = (
    "primary_used_pct",
    "primary_remaining_pct",
    "primary_reset",
    "secondary_used_pct",
    "secondary_remaining_pct",
    "secondary_reset",
    "tokens_total_day",
    "input_tokens_day",
    "output_tokens_day",
    "cached_tokens_day",
    "messages_total_day",
    "active_hours_day",
)

CODEX_MESSAGE_ROLES = {"assistant", "system", "tool", "user"}
CODEX_MESSAGE_TYPES = {"assistant_message", "message", "system_message", "tool_message", "user_message"}
CODEX_BOOKKEEPING_TYPES = {"token_count"}


def empty_claude_code_stats() -> dict:
    return {
        "total_sessions": None,
        "total_messages": None,
        "models_used": [],
        "favorite_model": None,
        "total_tokens_by_model": {},
        "longest_session": None,
        "hour_counts": {},
        "daily_activity": [],
        "daily_tokens": [],
    }


def shape_claude_code_stats(raw: dict | None) -> dict:
    if not raw:
        return empty_claude_code_stats()

    daily = raw.get("dailyActivity", [])
    model_tokens = raw.get("dailyModelTokens", [])
    models = raw.get("modelUsage", [])
    longest = raw.get("longestSession") or {}

    model_totals: dict[str, int] = {}
    for entry in model_tokens:
        for model, tokens in (entry.get("tokensByModel") or {}).items():
            model_totals[model] = model_totals.get(model, 0) + int(tokens or 0)
    favorite = max(model_totals, key=model_totals.get) if model_totals else None
    favorite_display = favorite.replace("claude-", "").replace("-", " ").title() if favorite else None

    return {
        "total_sessions": raw.get("totalSessions"),
        "total_messages": raw.get("totalMessages"),
        "models_used": list(models.keys()) if isinstance(models, dict) else (models if isinstance(models, list) else []),
        "favorite_model": favorite_display,
        "total_tokens_by_model": model_totals,
        "longest_session": _format_duration_ms(longest.get("duration")),
        "hour_counts": raw.get("hourCounts", {}) or {},
        "daily_activity": [
            {
                "date": entry["date"],
                "messages": entry.get("messageCount", 0),
                "sessions": entry.get("sessionCount", 0),
                "tool_calls": entry.get("toolCallCount", 0),
            }
            for entry in daily
            if isinstance(entry, dict) and entry.get("date")
        ],
        "daily_tokens": [
            {
                "date": entry["date"],
                "tokens_by_model": entry.get("tokensByModel", {}) or {},
            }
            for entry in model_tokens
            if isinstance(entry, dict) and entry.get("date")
        ],
    }


def empty_codex_thread_stats() -> dict:
    return {
        "total_tokens": None,
        "total_threads": None,
        "by_model": {},
        "recent_threads": [],
        "today_tokens": None,
        "today_threads": None,
        "today_by_model": {},
    }


def scan_codex_thread_stats(db_path: Path | None = None, now: datetime | None = None) -> dict:
    path = db_path or (Path.home() / ".codex" / "state_5.sqlite")
    if not path.exists():
        return empty_codex_thread_stats()

    now_local = now or datetime.now()
    today_start = int(datetime(now_local.year, now_local.month, now_local.day).timestamp())
    try:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("SELECT SUM(tokens_used), COUNT(*) FROM threads").fetchone()
            model_rows = conn.execute(
                "SELECT model, SUM(tokens_used), COUNT(*) FROM threads GROUP BY model"
            ).fetchall()
            recent = conn.execute(
                """SELECT title, tokens_used, model, source,
                          datetime(updated_at, 'unixepoch', 'localtime')
                   FROM threads ORDER BY updated_at DESC LIMIT 5"""
            ).fetchall()
            today_row = conn.execute(
                "SELECT SUM(tokens_used), COUNT(*) FROM threads WHERE updated_at >= ?",
                (today_start,),
            ).fetchone()
            today_models = conn.execute(
                "SELECT model, SUM(tokens_used), COUNT(*) FROM threads WHERE updated_at >= ? GROUP BY model",
                (today_start,),
            ).fetchall()
            source_rows = conn.execute(
                "SELECT source, SUM(tokens_used), COUNT(*) FROM threads GROUP BY source"
            ).fetchall()
    except Exception:
        return empty_codex_thread_stats()

    total_tokens = int(row[0] or 0)
    total_threads = int(row[1] or 0)

    return {
        "total_tokens": total_tokens,
        "total_threads": total_threads,
        "by_model": {
            model or "unknown": {
                "tokens": int(tokens or 0),
                "threads": int(threads or 0),
            }
            for model, tokens, threads in model_rows
        },
        "recent_threads": [
            {
                "title": (title or "")[:60],
                "tokens": int(tokens or 0),
                "model": model,
                "source": source,
                "updated": updated,
            }
            for title, tokens, model, source, updated in recent
        ],
        "today_tokens": int((today_row[0] if today_row else 0) or 0),
        "today_threads": int((today_row[1] if today_row else 0) or 0),
        "today_by_model": {
            model or "unknown": {
                "tokens": int(tokens or 0),
                "threads": int(threads or 0),
            }
            for model, tokens, threads in today_models
        },
        "by_source": {
            (source or "unknown"): {
                "tokens": int(tokens or 0),
                "sessions": int(threads or 0),
            }
            for source, tokens, threads in source_rows
        },
    }


def empty_codex_session_stats() -> dict:
    return {
        "active_hours_today": None,
        "messages_today": None,
        "sessions_today": None,
        "total_sessions": None,
        "input_tokens_today": None,
        "output_tokens_today": None,
        "reasoning_tokens_today": None,
        "cached_tokens_today": None,
        "user_messages_today": None,
        "events_scanned": None,
    }


def scan_codex_session_metrics(sessions_root: Path | None = None, now: datetime | None = None) -> dict:
    if sessions_root is not None:
        session_files = sorted(sessions_root.glob("*/*/*/*.jsonl")) if sessions_root.exists() else []
    else:
        from src.codex_sources import codex_session_files

        session_files = list(codex_session_files())
    if not session_files:
        return empty_codex_session_stats()
    now_local = now or datetime.now()
    start_utc, end_utc = _local_day_bounds(now_local)
    user_timestamps: list[datetime] = []
    total_messages = 0
    total_input = 0
    total_output = 0
    total_reasoning = 0
    total_cached = 0
    active_sessions_today = 0
    events_scanned = 0

    for session_file in session_files:
        session_has_today = False
        try:
            with open(session_file) as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    timestamp = _parse_event_timestamp(entry.get("timestamp"))
                    if timestamp is None or not (start_utc <= timestamp < end_utc):
                        continue

                    session_has_today = True
                    events_scanned += 1

                    if _is_codex_message_payload(payload):
                        total_messages += 1
                        if _is_codex_user_payload(payload):
                            user_timestamps.append(timestamp.astimezone())
                    if payload.get("type") in CODEX_BOOKKEEPING_TYPES:
                        last_usage = ((payload.get("info") or {}).get("last_token_usage") or {})
                        if not isinstance(last_usage, dict):
                            continue
                        total_input += int(last_usage.get("input_tokens") or 0)
                        total_output += int(last_usage.get("output_tokens") or 0)
                        total_reasoning += int(last_usage.get("reasoning_output_tokens") or 0)
                        total_cached += int(last_usage.get("cached_input_tokens") or 0)
        except OSError:
            continue

        if session_has_today:
            active_sessions_today += 1

    user_timestamps.sort()
    return {
        "active_hours_today": round(_active_hours_from_timestamps(user_timestamps), 1),
        "messages_today": total_messages,
        "sessions_today": active_sessions_today,
        "total_sessions": len(session_files),
        "input_tokens_today": total_input,
        "output_tokens_today": total_output,
        "reasoning_tokens_today": total_reasoning,
        "cached_tokens_today": total_cached,
        "user_messages_today": len(user_timestamps),
        "events_scanned": events_scanned,
    }


def _local_day_bounds(now_local: datetime) -> tuple[datetime, datetime]:
    local_start = datetime(now_local.year, now_local.month, now_local.day)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc, end_utc


def _parse_event_timestamp(raw_timestamp: object) -> datetime | None:
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return None
    try:
        return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_codex_message_payload(payload: dict) -> bool:
    role = str(payload.get("role") or "").strip().lower()
    payload_type = str(payload.get("type") or "").strip().lower()
    if payload_type in CODEX_BOOKKEEPING_TYPES:
        return False
    if role in CODEX_MESSAGE_ROLES:
        return True
    return payload_type in CODEX_MESSAGE_TYPES


def _is_codex_user_payload(payload: dict) -> bool:
    role = str(payload.get("role") or "").strip().lower()
    payload_type = str(payload.get("type") or "").strip().lower()
    return role == "user" or payload_type == "user_message"


def _format_duration_ms(duration_ms: object) -> str | None:
    if duration_ms is None:
        return None
    duration = int(duration_ms or 0)
    days = duration // 86400000
    hours = (duration % 86400000) // 3600000
    minutes = (duration % 3600000) // 60000
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def provider_registry() -> list[dict[str, object]]:
    return [dict(item) for item in sorted(PROVIDER_REGISTRY, key=lambda item: int(item["order"]))]


def empty_provider_shared() -> dict[str, Any]:
    return {field: None for field in SNAPSHOT_SHARED_FIELDS}


def normalize_provider_snapshot(
    *,
    provider: str,
    timestamp: int,
    status: str,
    shared: dict[str, Any] | None = None,
    unique: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    error_text: str | None = None,
    plan: str | None = None,
) -> dict[str, Any]:
    provider_id = str(provider).strip().lower()
    if provider_id not in PROVIDER_IDS:
        raise ValueError(f"Unknown provider: {provider}")
    if status not in PROVIDER_SNAPSHOT_STATUSES:
        raise ValueError(f"Invalid snapshot status: {status}")

    normalized_shared = empty_provider_shared()
    if isinstance(shared, dict):
        for key, value in shared.items():
            if key in normalized_shared:
                normalized_shared[key] = value
    return {
        "provider": provider_id,
        "timestamp": int(timestamp),
        "status": status,
        "plan": plan,
        "shared": normalized_shared,
        "unique": dict(unique or {}),
        "source": dict(source or {}),
        "error_text": error_text,
    }


def build_provider_snapshots(
    *,
    timestamp: int,
    collector_access: str,
    messages: dict | None,
    tokens: dict | None,
    usage: dict | None,
    codex_local: dict | None,
    codex_sessions: dict | None,
    codex_usage: dict | None,
    cursor_usage: dict | None,
    errors: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    errors = errors or {}
    snapshots: dict[str, dict[str, Any]] = {}
    plans = load_plans()

    def _status(provider: str, has_data: bool, complete: bool) -> tuple[str, str | None]:
        error_text = errors.get(provider)
        if error_text:
            return "error", error_text
        if complete:
            return "ok", None
        if has_data:
            return "partial", None
        return "stale", None

    # Claude
    claude_has_data = any(item for item in (messages, tokens, usage))
    claude_complete = bool(messages and tokens and usage)
    claude_status, claude_error = _status("claude", claude_has_data, claude_complete)
    claude_shared = empty_provider_shared()
    session_used = _as_float((usage or {}).get("session_pct"))
    weekly_used = _as_float((usage or {}).get("weekly_pct"))
    claude_shared.update(
        {
            "primary_used_pct": session_used,
            "primary_remaining_pct": _remaining_pct(session_used),
            "primary_reset": (usage or {}).get("session_reset"),
            "secondary_used_pct": weekly_used,
            "secondary_remaining_pct": _remaining_pct(weekly_used),
            "secondary_reset": (usage or {}).get("weekly_reset"),
            "tokens_total_day": _claude_total_tokens_day(tokens),
            "input_tokens_day": _as_float((tokens or {}).get("input_tokens")),
            "output_tokens_day": _as_float((tokens or {}).get("output_tokens")),
            "cached_tokens_day": _as_float(
                int((tokens or {}).get("cache_read_tokens") or 0)
                + int((tokens or {}).get("cache_create_tokens") or 0)
            ) if tokens else None,
            "messages_total_day": _as_float((messages or {}).get("total_messages")),
            "active_hours_day": _as_float((messages or {}).get("active_hours")),
        }
    )
    claude_unique: dict[str, Any] = {}
    claude_unique["limits"] = (usage or {}).get("limits") or []
    claude_unique["credit_pools"] = (usage or {}).get("credit_pools") or []
    claude_plan = resolve_plan("claude", (usage or {}).get("plan_id"), plans=plans)
    if (usage or {}).get("plan_source"):
        claude_plan["plan_source"] = usage["plan_source"]
    if (usage or {}).get("raw_plan"):
        claude_plan["raw_plan"] = usage["raw_plan"]
    claude_unique["plan"] = claude_plan.get("plan_label")
    if isinstance(messages, dict):
        claude_unique["conversations_today"] = messages.get("conversations")
    snapshots["claude"] = normalize_provider_snapshot(
        provider="claude",
        timestamp=timestamp,
        status=claude_status,
        shared=claude_shared,
        unique=claude_unique,
        plan=claude_plan.get("plan_label"),
        source={
            "collector_access": collector_access,
            "raw_refs": _raw_refs(
                messages=messages,
                tokens=tokens,
                usage=usage,
            ),
            "plan_id": claude_plan.get("plan_id"),
            "plan_source": claude_plan.get("plan_source"),
        },
        error_text=claude_error,
    )

    # Codex
    codex_has_data = any(item for item in (codex_local, codex_sessions, codex_usage))
    codex_complete = bool(codex_local and codex_sessions and codex_usage)
    codex_status, codex_error = _status("codex", codex_has_data, codex_complete)
    codex_shared = empty_provider_shared()
    codex_session_used = _used_from_remaining((codex_usage or {}).get("session_remaining_pct"))
    codex_weekly_used = _used_from_remaining((codex_usage or {}).get("weekly_remaining_pct"))
    codex_shared.update(
        {
            "primary_used_pct": codex_session_used,
            "primary_remaining_pct": _as_float((codex_usage or {}).get("session_remaining_pct")),
            "primary_reset": (codex_usage or {}).get("session_reset"),
            "secondary_used_pct": codex_weekly_used,
            "secondary_remaining_pct": _as_float((codex_usage or {}).get("weekly_remaining_pct")),
            "secondary_reset": (codex_usage or {}).get("weekly_reset") or (codex_usage or {}).get("reset_at"),
            "tokens_total_day": _codex_total_tokens_day(codex_sessions),
            "input_tokens_day": _as_float((codex_sessions or {}).get("input_tokens_today")),
            "output_tokens_day": _as_float((codex_sessions or {}).get("output_tokens_today")),
            "cached_tokens_day": _as_float((codex_sessions or {}).get("cached_tokens_today")),
            "messages_total_day": _as_float((codex_sessions or {}).get("messages_today")),
            "active_hours_day": _as_float((codex_sessions or {}).get("active_hours_today")),
        }
    )
    codex_unique = {
        "threads_today": (codex_local or {}).get("today_threads"),
        "sessions_today": (codex_sessions or {}).get("sessions_today"),
        "reasoning_tokens_today": (codex_sessions or {}).get("reasoning_tokens_today"),
        "code_review_used_pct": _used_from_remaining((codex_usage or {}).get("code_review_remaining_pct")),
        "total_threads": (codex_local or {}).get("total_threads"),
        "total_sessions": (codex_sessions or {}).get("total_sessions"),
        "limits": (codex_usage or {}).get("limits") or [],
        "credit_pools": (codex_usage or {}).get("credit_pools") or [],
    }
    codex_plan = resolve_plan("codex", (codex_usage or {}).get("plan_id"), plans=plans)
    if (codex_usage or {}).get("plan_source"):
        codex_plan["plan_source"] = codex_usage["plan_source"]
    if (codex_usage or {}).get("raw_plan"):
        codex_plan["raw_plan"] = codex_usage["raw_plan"]
    codex_unique["plan"] = codex_plan.get("plan_label")
    snapshots["codex"] = normalize_provider_snapshot(
        provider="codex",
        timestamp=timestamp,
        status=codex_status,
        shared=codex_shared,
        unique=codex_unique,
        plan=codex_plan.get("plan_label"),
        source={
            "collector_access": collector_access,
            "raw_refs": _raw_refs(
                codex_local=codex_local,
                codex_sessions=codex_sessions,
                codex_usage=codex_usage,
            ),
            "plan_id": codex_plan.get("plan_id"),
            "plan_source": codex_plan.get("plan_source"),
        },
        error_text=codex_error,
    )

    # Cursor
    cursor_has_data = bool(cursor_usage)
    cursor_status, cursor_error = _status("cursor", cursor_has_data, cursor_has_data)
    cursor_used = _cursor_used_pct(cursor_usage or {})
    cursor_remaining = _as_float((cursor_usage or {}).get("remaining_requests"))
    cursor_shared = empty_provider_shared()
    cursor_shared.update(
        {
            "primary_used_pct": cursor_used,
            "primary_remaining_pct": (
                0.0
                if cursor_remaining == 0
                else _remaining_pct(cursor_used)
            ),
            "primary_reset": (cursor_usage or {}).get("reset_at") or (cursor_usage or {}).get("reset"),
            "tokens_total_day": _as_float((cursor_usage or {}).get("total_tokens")),
            "messages_total_day": _as_float((cursor_usage or {}).get("total_requests")),
        }
    )
    snapshots["cursor"] = normalize_provider_snapshot(
        provider="cursor",
        timestamp=timestamp,
        status=cursor_status,
        shared=cursor_shared,
        unique={
            "plan": (cursor_usage or {}).get("plan"),
            "total_requests": (cursor_usage or {}).get("total_requests"),
            "total_tokens": (cursor_usage or {}).get("total_tokens"),
            "max_requests": (cursor_usage or {}).get("max_requests"),
            "remaining_requests": (cursor_usage or {}).get("remaining_requests"),
            "at_limit": (cursor_usage or {}).get("at_limit"),
            "limit_hit": (cursor_usage or {}).get("limit_hit"),
            "limit_kind": (cursor_usage or {}).get("limit_kind"),
            "limit_message": (cursor_usage or {}).get("limit_message"),
            "reset_at": (cursor_usage or {}).get("reset_at"),
            "spend_limit_hit": (cursor_usage or {}).get("spend_limit_hit"),
            "spend_limits": (cursor_usage or {}).get("spend_limits"),
            "models": (cursor_usage or {}).get("models"),
        },
        source={
            "collector_access": collector_access,
            "raw_refs": _raw_refs(cursor_usage=cursor_usage),
        },
        error_text=cursor_error,
    )

    return snapshots


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _remaining_pct(used_pct: float | None) -> float | None:
    if used_pct is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - used_pct)), 3)


def _used_from_remaining(remaining_pct: Any) -> float | None:
    remaining = _as_float(remaining_pct)
    if remaining is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - remaining)), 3)


def _claude_total_tokens_day(tokens: dict | None) -> float | None:
    if not isinstance(tokens, dict):
        return None
    return _as_float(
        int(tokens.get("input_tokens") or 0)
        + int(tokens.get("output_tokens") or 0)
        + int(tokens.get("cache_read_tokens") or 0)
        + int(tokens.get("cache_create_tokens") or 0)
    )


def _codex_total_tokens_day(codex_sessions: dict | None) -> float | None:
    if not isinstance(codex_sessions, dict):
        return None
    return _as_float(
        int(codex_sessions.get("input_tokens_today") or 0)
        + int(codex_sessions.get("output_tokens_today") or 0)
        + int(codex_sessions.get("reasoning_tokens_today") or 0)
    )


def _cursor_used_pct(cursor_usage: dict) -> float | None:
    if not isinstance(cursor_usage, dict):
        return None
    if cursor_usage.get("at_limit") or cursor_usage.get("limit_hit"):
        return 100.0
    models = cursor_usage.get("models") or {}
    if isinstance(models, dict):
        max_total = 0
        used_total = 0
        for model_data in models.values():
            if not isinstance(model_data, dict):
                continue
            max_requests = model_data.get("max_requests")
            requests = model_data.get("requests")
            if isinstance(max_requests, (int, float)) and max_requests > 0:
                max_total += int(max_requests)
                used_total += int(requests or 0)
        if max_total > 0:
            return round((used_total / max_total) * 100, 3)

    max_requests = cursor_usage.get("max_requests")
    total_requests = cursor_usage.get("total_requests")
    if isinstance(max_requests, (int, float)) and max_requests > 0 and isinstance(total_requests, (int, float)):
        return round((float(total_requests) / float(max_requests)) * 100, 3)
    return None


def _raw_refs(**values: Any) -> list[str]:
    refs: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        refs.append(key)
    return refs
