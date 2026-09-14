"""Shared JSONL/file scanners for Claude Code session data.

Used by both collector.py (primary) and database.py (fallback).
Single source of truth for today's message, token, and project scanning.
"""

import glob
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.claude_sources import claude_event_identity

ACTIVITY_GAP_SEC = 1800  # 30 min gap = new activity block

def claude_jsonl_files() -> list[str]:
    """All Claude session JSONL files across CLI and desktop-app roots.

    The CLI writes to ~/.claude/projects; the desktop app (Cowork sessions)
    writes the same format under per-session .claude/projects trees in
    Application Support. Roots are resolved at call time so tests can patch
    Path.home().
    """
    cli_root = Path.home() / ".claude" / "projects"
    desktop_root = (
        Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    )
    files = glob.glob(str(cli_root / "**" / "*.jsonl"), recursive=True)
    files.extend(
        glob.glob(
            str(desktop_root / "**" / ".claude" / "projects" / "**" / "*.jsonl"),
            recursive=True,
        )
    )
    return sorted(set(files))


LOCAL_COMMAND_MARKERS = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)


def _today_boundaries() -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for today in local time."""
    local_now = datetime.now()
    local_start = datetime(local_now.year, local_now.month, local_now.day)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc, end_utc


def _active_hours_from_timestamps(timestamps: list[datetime], gap_sec: int = ACTIVITY_GAP_SEC) -> float:
    """Compute keyboard-active hours from sorted timestamps using gap-based blocks.

    Splits into blocks separated by gap_sec gaps (default ACTIVITY_GAP_SEC).
    Returns hours from first to last timestamp within each block.
    """
    if len(timestamps) < 2:
        return 0.0
    active_seconds = 0.0
    block = [timestamps[0]]
    for t in timestamps[1:]:
        if (t - block[-1]).total_seconds() > gap_sec:
            if len(block) >= 2:
                active_seconds += (block[-1] - block[0]).total_seconds()
            block = [t]
        else:
            block.append(t)
    if len(block) >= 2:
        active_seconds += (block[-1] - block[0]).total_seconds()
    return active_seconds / 3600


def _message_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        nested = item.get("content")
        if isinstance(nested, str):
            parts.append(nested)
    return "\n".join(parts)


def _is_local_command_message(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    text = _message_content_text(msg.get("content"))
    return any(marker in text for marker in LOCAL_COMMAND_MARKERS)


def _countable_claude_message(entry: dict) -> tuple[bool, str | None]:
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return False, None

    role = msg.get("role")
    if role not in {"user", "assistant"}:
        return False, None
    if _is_local_command_message(msg):
        return False, None
    return True, role


def scan_cc_messages_today() -> dict:
    """Scan Claude Code JSONL files for today's messages and active time.

    Active hours are computed from user-role messages only (keyboard time),
    using 30-minute gaps to separate activity blocks.
    """
    start_utc, end_utc = _today_boundaries()
    message_timestamps = []
    user_timestamps = []
    active_files = set()
    active_sessions = set()
    seen_messages: set[str] = set()

    for f in claude_jsonl_files():
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp", "")
                        if not isinstance(ts, str) or len(ts) < 10:
                            continue
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if start_utc <= dt < end_utc:
                            countable, role = _countable_claude_message(e)
                            if not countable:
                                continue
                            identity = claude_event_identity(e)
                            if identity and identity in seen_messages:
                                continue
                            if identity:
                                seen_messages.add(identity)
                            message_timestamps.append(dt.astimezone())
                            active_files.add(f)
                            active_sessions.add(str(e.get("sessionId") or f))
                            if role == "user":
                                user_timestamps.append(dt.astimezone())
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            pass

    if not message_timestamps:
        return {"total_messages": 0, "active_hours": 0.0, "conversations": 0, "by_hour": {}}

    message_timestamps.sort()
    user_timestamps.sort()

    by_hour: dict[str, int] = {}
    for t in message_timestamps:
        h = t.strftime("%H")
        by_hour[h] = by_hour.get(h, 0) + 1

    return {
        "total_messages": len(message_timestamps),
        "first": message_timestamps[0].strftime("%H:%M"),
        "last": message_timestamps[-1].strftime("%H:%M"),
        "active_hours": round(_active_hours_from_timestamps(user_timestamps), 1),
        "conversations": len(active_sessions or active_files),
        "by_hour": by_hour,
    }


def scan_cc_tokens_today() -> dict:
    """Scan Claude Code JSONL files for today's token usage per model."""
    start_utc, end_utc = _today_boundaries()
    models: dict[str, dict] = {}
    total_input = total_output = total_cache_read = total_cache_create = 0
    seen_usage: set[str] = set()

    for f in claude_jsonl_files():
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp", "")
                        if not isinstance(ts, str) or len(ts) < 10:
                            continue
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if not (start_utc <= dt < end_utc):
                            continue
                        msg = e.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        usage_key = claude_event_identity(e)
                        if usage_key:
                            if usage_key in seen_usage:
                                continue
                            seen_usage.add(usage_key)
                        model = msg.get("model", "unknown")
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cr = usage.get("cache_read_input_tokens", 0)
                        cc = usage.get("cache_creation_input_tokens", 0)
                        total_input += inp
                        total_output += out
                        total_cache_read += cr
                        total_cache_create += cc
                        if model not in models:
                            models[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "requests": 0}
                        models[model]["input"] += inp
                        models[model]["output"] += out
                        models[model]["cache_read"] += cr
                        models[model]["cache_create"] += cc
                        models[model]["requests"] += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            pass

    total = total_input + total_output
    cache_total = total_cache_read + total_cache_create
    return {
        "total_tokens": total,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_create_tokens": total_cache_create,
        "cache_hit_pct": round(total_cache_read / cache_total * 100, 1) if cache_total > 0 else 0.0,
        "by_model": models,
    }


def scan_cc_daily_model_tokens(days: int = 30) -> list[dict]:
    """Scan recent Claude Code JSONL files for daily input+output tokens by model."""
    local_now = datetime.now().astimezone()
    local_start = datetime(local_now.year, local_now.month, local_now.day).astimezone()
    cutoff = local_start - timedelta(days=max(days, 1) - 1)
    by_day: dict[str, dict[str, int]] = {}
    seen_usage: set[str] = set()

    for f in claude_jsonl_files():
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp", "")
                        if not isinstance(ts, str) or len(ts) < 10:
                            continue
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                        except ValueError:
                            continue
                        if dt < cutoff:
                            continue
                        msg = e.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        usage_key = claude_event_identity(e)
                        if usage_key:
                            if usage_key in seen_usage:
                                continue
                            seen_usage.add(usage_key)
                        tokens = (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
                        if tokens <= 0:
                            continue
                        day = dt.date().isoformat()
                        model = msg.get("model", "unknown")
                        by_day.setdefault(day, {})
                        by_day[day][model] = by_day[day].get(model, 0) + tokens
                    except (json.JSONDecodeError, TypeError):
                        pass
        except OSError:
            pass

    return [
        {"date": day, "tokens_by_model": models}
        for day, models in sorted(by_day.items())
    ]


def scan_cc_projects_today() -> list[dict]:
    """Scan Claude Code JSONL files for per-project active hours today."""
    start_utc, end_utc = _today_boundaries()
    project_ts: dict[str, list] = {}
    seen_messages: set[str] = set()

    for f in claude_jsonl_files():
        project = Path(f).parent.name
        try:
            with open(f) as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        ts = e.get("timestamp", "")
                        if not isinstance(ts, str) or len(ts) < 10:
                            continue
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if start_utc <= dt < end_utc:
                            countable, _ = _countable_claude_message(e)
                            if not countable:
                                continue
                            identity = claude_event_identity(e)
                            if identity and identity in seen_messages:
                                continue
                            if identity:
                                seen_messages.add(identity)
                            if project not in project_ts:
                                project_ts[project] = []
                            project_ts[project].append(dt.astimezone())
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            pass

    result = []
    for project, timestamps in project_ts.items():
        if not timestamps:
            continue
        timestamps.sort()
        active_hours = round(_active_hours_from_timestamps(timestamps, gap_sec=900), 1)
        # Claude keys a project folder by its absolute path with "/" turned into "-".
        # Fold the user's own home prefix to "~/" so the label is short and portable.
        home_key = str(Path.home()).replace("/", "-")
        if project.startswith(home_key + "-"):
            name = "~/" + project[len(home_key) + 1:].replace("-", "/")
        else:
            name = project.replace("-", "/")
        result.append({
            "project": name,
            "active_hours": active_hours,
            "messages": len(timestamps),
        })

    return sorted(result, key=lambda x: -x["active_hours"])
