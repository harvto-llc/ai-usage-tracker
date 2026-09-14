"""Gap rollups for the /stats endpoint.

Extracted from src/session_analytics.py (gap classification over the merged
Claude Code JSONL activity timeline, daily gap-state bucketing, and the
today/yesterday/last_7d rollup consumed by the API). Only the pipeline needed
by ``gap_rollups_for_stats`` is kept; helpers are copied verbatim from the
original module.
"""

from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.working_hours import WEEKDAY_KEYS, WorkingHours, load_or_infer_working_hours

_cache: dict[int, dict] = {}
_cache_ts: dict[int, float] = {}
_CACHE_TTL = 300  # 5 min
WORK_SESSION_BREAK_MS = 45 * 60 * 1000
FOCUS_GAP_MS = 5 * 60 * 1000
AUTONOMOUS_IDLE_BREAK_MS = 30 * 60 * 1000
TRIVIAL_SESSION_BYTES = 5 * 1024

PROJECTS_CONFIG = Path.home() / ".usage-tracker" / "projects.toml"


def _load_projects_config() -> tuple[dict[str, Path], dict[str, str]]:
    """Optional per-user project map, read from ~/.usage-tracker/projects.toml.

    [repos]    project-key = "~/path/to/checkout"   (used when a session names no path)
    [aliases]  folder-name = "project-key"           (folds a worktree or fork into its project)

    Both default to empty: without the file every project is keyed by its own folder
    name, which is the right answer for a fresh install. The file is never written by
    the tracker.
    """
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return {}, {}
    try:
        with open(PROJECTS_CONFIG, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}, {}
    repos = {
        str(key): Path(str(value)).expanduser()
        for key, value in (data.get("repos") or {}).items()
        if isinstance(value, str) and value.strip()
    }
    aliases = {
        str(key): str(value)
        for key, value in (data.get("aliases") or {}).items()
        if isinstance(value, str) and value.strip()
    }
    return repos, aliases


KNOWN_PROJECT_REPOS, PROJECT_ALIASES = _load_projects_config()


class GapSegment(TypedDict):
    start: datetime
    end: datetime
    state: str
    ms: int
    session_attributed: bool


class GapClassification(TypedDict):
    state: str
    focus_ms: int
    attention_idle_ms: int
    off_hours_ms: int
    agent_runtime_ms: int
    work_session_break: bool
    segments: list[GapSegment]


def session_analytics(days: int = 7) -> dict:
    """Compute daily gap-state rollup rows from recent JSONL data.

    Trimmed from the full session-analytics pipeline: it keeps exactly the
    aggregation that feeds ``gap_rollups_for_stats`` — per-day gap-state
    buckets classified over the merged activity timeline — and drops the
    payload sections (tools, models, drilldowns, stories, work sessions)
    that only served other session-analytics features.
    """
    global _cache, _cache_ts
    period_days = _normalize_period_days(days)
    now = time.time()
    if period_days in _cache and (now - _cache_ts.get(period_days, 0)) < _CACHE_TTL:
        return _cache[period_days]

    cutoff = _period_cutoff(period_days)
    from src.scanners import claude_jsonl_files
    files = sorted(claude_jsonl_files())

    all_turns: list[dict] = []
    session_results: list[dict] = []
    daily_stats: dict[str, dict] = defaultdict(lambda: {
        "turns": 0,
        "focus_gap_sec": 0.0, "attention_idle_sec": 0.0,
        "off_hours_away_sec": 0.0, "agent_runtime_sec": 0.0,
        "downtime_events": 0,
        "sessions": 0,
    })

    for f in files:
        try:
            if _session_source_metadata(f).get("classification") != "interactive":
                continue
            session = _analyze_session(f, cutoff)
            if not session or session["turn_count"] == 0:
                continue
            session_results.append(session)
            all_turns.extend(session["turns"])
        except Exception:
            pass

    local_tz = _local_timezone()
    local_now = datetime.now(local_tz)
    human_timestamps = [turn["human_ts"] for turn in all_turns if turn.get("human_ts")]
    working_hours = load_or_infer_working_hours(human_timestamps, local_now, local_tz)
    _apply_global_gap_classification(all_turns, working_hours, local_tz)

    for session in session_results:
        day = session["summary"]["date"]
        daily_stats[day]["sessions"] += 1

    for turn in all_turns:
        turn_day = turn.get("human_ts")
        if turn_day:
            td = turn_day.astimezone(local_tz).strftime("%Y-%m-%d") if hasattr(turn_day, "astimezone") else ""
        else:
            td = ""
        if not td:
            continue

        daily_stats[td]["turns"] += 1
        _add_gap_segments_to_daily(daily_stats, turn, local_tz)

    result = {
        "period_days": period_days,
        "period_label": _period_label(period_days),
        "sessions_analyzed": len(session_results),
        "total_turns": len(all_turns),
        "generated_at": int(time.time()),
        "working_hours": working_hours,

        # Daily gap-state rows (with legacy human_time/downtime compat keys)
        "daily": [
            {
                "date": day,
                "turns": stats["turns"],
                "focus_gap_sec": round(stats["focus_gap_sec"]),
                "attention_idle_sec": round(stats["attention_idle_sec"]),
                "off_hours_away_sec": round(stats["off_hours_away_sec"]),
                "agent_runtime_sec": round(stats["agent_runtime_sec"]),
                "human_time_sec": round(stats["focus_gap_sec"] + stats["attention_idle_sec"]),
                "downtime_sec": 0,
                "downtime_events": 0,
                "sessions": stats["sessions"],
            }
            for day, stats in sorted(daily_stats.items())
        ],
    }

    _cache[period_days] = result
    _cache_ts[period_days] = now
    return result


_GAP_ROLLUP_KEYS = (
    "focus_gap_sec",
    "attention_idle_sec",
    "off_hours_away_sec",
    "agent_runtime_sec",
)


def _empty_gap_rollup() -> dict:
    return {
        "focus_gap_sec": 0,
        "attention_idle_sec": 0,
        "off_hours_away_sec": 0,
        "agent_runtime_sec": 0,
        # Legacy: human_time_sec = focus + attention. downtime_sec is retired
        # under the 4-state model but kept here at zero for older consumers.
        "human_time_sec": 0,
        "downtime_sec": 0,
    }


def _finalize_gap_rollup(rollup: dict) -> dict:
    for key in _GAP_ROLLUP_KEYS:
        rollup[key] = int(round(rollup.get(key, 0) or 0))
    rollup["human_time_sec"] = rollup["focus_gap_sec"] + rollup["attention_idle_sec"]
    rollup["downtime_sec"] = 0
    return rollup


def gap_rollups_for_stats() -> dict:
    """Roll up 4-state gap fields over today / yesterday / last_7d.

    Reuses ``session_analytics(7)`` so the same aggregation that powers
    ``/analytics/sessions`` is the single source of truth. Result shape:

        {
          "today":     {focus_gap_sec, attention_idle_sec, off_hours_away_sec,
                        agent_runtime_sec, human_time_sec, downtime_sec},
          "yesterday": { ... },
          "last_7d":   { ... },
          "today_date": "YYYY-MM-DD",       # local-tz date used for lookup
          "yesterday_date": "YYYY-MM-DD",
        }
    """
    today = _empty_gap_rollup()
    yesterday = _empty_gap_rollup()
    last_7d = _empty_gap_rollup()

    local_tz = _local_timezone()
    local_now = datetime.now(local_tz)
    today_key = local_now.strftime("%Y-%m-%d")
    yesterday_key = (local_now - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        analytics = session_analytics(7)
    except Exception:
        analytics = {}

    for row in analytics.get("daily") or []:
        for key in _GAP_ROLLUP_KEYS:
            value = row.get(key, 0) or 0
            last_7d[key] = last_7d.get(key, 0) + value
        if row.get("date") == today_key:
            for key in _GAP_ROLLUP_KEYS:
                today[key] = row.get(key, 0) or 0
        elif row.get("date") == yesterday_key:
            for key in _GAP_ROLLUP_KEYS:
                yesterday[key] = row.get(key, 0) or 0

    return {
        "today": _finalize_gap_rollup(today),
        "yesterday": _finalize_gap_rollup(yesterday),
        "last_7d": _finalize_gap_rollup(last_7d),
        "today_date": today_key,
        "yesterday_date": yesterday_key,
    }


def _normalize_period_days(days: int | str | None) -> int:
    try:
        value = int(days if days is not None else 7)
    except (TypeError, ValueError):
        value = 7
    return max(0, value)


def _period_cutoff(days: int) -> datetime:
    if days <= 0:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=days)


def _period_label(days: int) -> str:
    return "all-time" if days <= 0 else f"{days}d"


def _local_timezone() -> tzinfo:
    for name in (_env_timezone_name(), _system_timezone_name()):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def _env_timezone_name() -> str | None:
    value = os.environ.get("TZ")
    if not value:
        return None
    return value.removeprefix(":")


def _system_timezone_name() -> str | None:
    try:
        path = Path("/etc/localtime").resolve()
    except OSError:
        return None
    parts = path.parts
    if "zoneinfo" not in parts:
        return None
    index = parts.index("zoneinfo") + 1
    name = "/".join(parts[index:])
    return name or None


def _message_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            value = item.get("text")
            if value is None:
                value = item.get("content")
            if value is not None:
                parts.append(str(value))
        return "\n".join(parts)
    return ""


def _analyze_session(path: str, cutoff: datetime) -> dict | None:
    """Analyze a single JSONL conversation file."""
    entries = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return None

    # Parse all timestamped entries
    parsed: list[dict] = []
    session_id = None
    cwd = None
    git_branch = None
    bridge_messages = 0
    user_turns = 0
    assistant_turns = 0
    total_input_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    service_tiers: set[str] = set()
    stop_reasons: dict[str, int] = defaultdict(int)
    seen_usage: set[str] = set()
    for e in entries:
        ts_str = e.get("timestamp", "")
        if not isinstance(ts_str, str) or len(ts_str) < 10:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue

        if not session_id:
            session_id = e.get("sessionId", "")
        if not cwd and isinstance(e.get("cwd"), str):
            cwd = e.get("cwd")
        if not git_branch and isinstance(e.get("gitBranch"), str):
            git_branch = e.get("gitBranch")

        etype = e.get("type", "")
        msg = e.get("message", {})
        role = msg.get("role", "") if isinstance(msg, dict) else ""
        content_text = _message_content_text(msg.get("content") if isinstance(msg, dict) else None)
        origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
        if role == "user" and (
            origin.get("kind") == "channel"
            or "loop-bridge" in str(origin)
            or "<channel " in content_text
            or "source_type=\"codex\"" in content_text
        ):
            bridge_messages += 1

        entry = {
            "ts": ts,
            "type": etype,
            "role": role,
            "prompt_id": e.get("promptId", ""),
            "request_id": e.get("requestId", ""),
        }

        if etype == "assistant" and isinstance(msg, dict):
            usage = msg.get("usage", {})
            usage_id = msg.get("id") or e.get("requestId")
            usage_is_new = not usage_id or str(usage_id) not in seen_usage
            if usage_id and usage_is_new:
                seen_usage.add(str(usage_id))
            entry["model"] = msg.get("model", "")
            entry["output_tokens"] = usage.get("output_tokens", 0) if usage_is_new else 0
            entry["input_tokens"] = usage.get("input_tokens", 0) if usage_is_new else 0
            entry["cache_read"] = usage.get("cache_read_input_tokens", 0) if usage_is_new else 0
            entry["cache_creation"] = usage.get("cache_creation_input_tokens", 0) if usage_is_new else 0
            entry["stop_reason"] = msg.get("stop_reason", "")
            entry["speed"] = usage.get("speed", "")
            entry["service_tier"] = msg.get("service_tier") or usage.get("service_tier") or usage.get("speed", "")
            assistant_turns += 1
            if usage_is_new:
                total_input_tokens += int(usage.get("input_tokens") or 0)
                cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
                cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
            if entry["service_tier"]:
                service_tiers.add(str(entry["service_tier"]))
            if entry["stop_reason"]:
                stop_reasons[str(entry["stop_reason"])] += 1

            # Extract tool names from content
            tools = []
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools.append(block.get("name", "unknown"))
            entry["tools"] = tools

        parsed.append(entry)

    if not parsed:
        return None

    # Build turns: a turn = human prompt → all agent responses until next human prompt
    turns: list[dict] = []
    tool_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ms": 0, "errors": 0})
    model_stats: dict[str, dict] = defaultdict(lambda: {
        "requests": 0, "output_tokens": 0, "input_tokens": 0,
        "total_latency_ms": 0, "tool_use_count": 0, "end_turn_count": 0,
    })

    current_turn: dict | None = None

    for entry in parsed:
        if entry["type"] == "user" and entry["role"] == "user":
            # Is this a real human prompt or a tool result?
            is_tool_result = bool(entry.get("prompt_id") and current_turn and current_turn.get("prompt_id") == entry.get("prompt_id"))

            if not is_tool_result:
                user_turns += 1
                # Finalize previous turn
                if current_turn and current_turn.get("agent_responses"):
                    _finalize_turn(current_turn, turns)

                # Start new turn
                current_turn = {
                    "human_ts": entry["ts"],
                    "session_id": session_id or Path(path).stem[:20],
                    "prompt_id": entry.get("prompt_id", ""),
                    "human_think_ms": 0,
                    "downtime_ms": 0,
                    "gap": None,
                    "gap_segments": [],
                    "focus_gap_ms": 0,
                    "attention_idle_ms": 0,
                    "off_hours_away_ms": 0,
                    "agent_runtime_ms": 0,
                    "work_session_break": False,
                    "agent_responses": [],
                    "tools_used": [],
                    "hour": entry["ts"].astimezone().hour,
                }

        elif entry["type"] == "assistant" and entry.get("output_tokens", 0) > 0:
            if current_turn is None:
                continue

            response_latency_ms = None
            if not current_turn["agent_responses"]:
                # First agent response — compute latency from human message
                lat = (entry["ts"] - current_turn["human_ts"]).total_seconds() * 1000
                if 0 < lat < 300_000:
                    response_latency_ms = round(lat)
                current_turn["response_latency_ms"] = response_latency_ms

            current_turn["agent_responses"].append(entry)

            # Track tools
            for tool in entry.get("tools", []):
                current_turn["tools_used"].append(tool)
                tool_stats[tool]["count"] += 1

            # Track model
            model = entry.get("model", "unknown")
            model_stats[model]["requests"] += 1
            model_stats[model]["output_tokens"] += entry.get("output_tokens", 0)
            model_stats[model]["input_tokens"] += entry.get("input_tokens", 0)
            if response_latency_ms:
                model_stats[model]["total_latency_ms"] += response_latency_ms
            if entry.get("stop_reason") == "tool_use":
                model_stats[model]["tool_use_count"] += 1
            elif entry.get("stop_reason") == "end_turn":
                model_stats[model]["end_turn_count"] += 1

    # Finalize last turn
    if current_turn and current_turn.get("agent_responses"):
        _finalize_turn(current_turn, turns)

    if not turns:
        return None

    # Session summary
    first_ts = min(t["human_ts"] for t in turns if "human_ts" in t)
    last_ts = max(t.get("last_agent_ts", t["human_ts"]) for t in turns)
    total_out = sum(t.get("total_output_tokens", 0) for t in turns)
    total_human = sum(t.get("human_think_ms", 0) or 0 for t in turns) / 1000
    total_agent = sum(t.get("turn_duration_ms", 0) or 0 for t in turns) / 1000
    total_downtime = sum(t.get("downtime_ms", 0) or 0 for t in turns) / 1000
    source_meta = _session_source_metadata(path, cwd)
    total_stop_reasons = sum(stop_reasons.values())
    agentic_ratio_pct = (
        round((stop_reasons.get("tool_use", 0) / total_stop_reasons) * 100, 1)
        if total_stop_reasons
        else 0
    )

    return {
        "turn_count": len(turns),
        "turns": turns,
        "tool_stats": dict(tool_stats),
        "model_stats": dict(model_stats),
        "summary": {
            "session_id": session_id or Path(path).stem[:20],
            "provider": "claude",
            "path": str(path),
            "project": source_meta["project"],
            "project_path": source_meta["project_path"],
            "repo": source_meta["repo"],
            "repo_path": source_meta["repo_path"],
            "file_size_bytes": source_meta["file_size_bytes"],
            "classification": source_meta["classification"],
            "git_branch": git_branch,
            "date": first_ts.astimezone().strftime("%Y-%m-%d"),
            "start": first_ts.isoformat(),
            "end": last_ts.isoformat(),
            "duration_min": round((last_ts - first_ts).total_seconds() / 60, 1),
            "turns": len(turns),
            "user_turns": user_turns,
            "assistant_turns": assistant_turns,
            "total_output_tokens": total_out,
            "total_input_tokens": total_input_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
            "gap_breakdown": {
                "focus_gap_sec": 0,
                "attention_idle_sec": 0,
                "off_hours_away_sec": 0,
                "agent_runtime_sec": 0,
            },
            "total_human_time_sec": round(total_human),
            "total_agent_time_sec": round(total_agent),
            "total_downtime_sec": round(total_downtime),
            "downtime_events": sum(1 for t in turns if t.get("downtime_ms")),
            "tool_calls": sum(len(t.get("tools_used", [])) for t in turns),
            "tool_counts": {
                name: stats["count"]
                for name, stats in sorted(tool_stats.items(), key=lambda item: -item[1]["count"])
            },
            "stop_reasons": dict(stop_reasons),
            "agentic_ratio_pct": agentic_ratio_pct,
            "bridge_messages": bridge_messages,
            "service_tiers": sorted(service_tiers),
            "models": list(set(
                r.get("model", "") for t in turns for r in t.get("agent_responses", []) if r.get("model")
            )),
            "avg_response_ms": round(
                sum(t.get("response_latency_ms", 0) or 0 for t in turns) /
                max(sum(1 for t in turns if t.get("response_latency_ms")), 1)
            ),
        },
    }


def _finalize_turn(turn: dict, turns_list: list):
    """Finalize a turn and add it to the list."""
    responses = turn["agent_responses"]
    if not responses:
        return

    first_ts = responses[0]["ts"]
    last_ts = responses[-1]["ts"]
    dur_ms = round((last_ts - first_ts).total_seconds() * 1000)

    turn_data = {
        "human_ts": turn["human_ts"],
        "first_agent_ts": first_ts,
        "last_agent_ts": last_ts,
        "session_id": turn.get("session_id", ""),
        "prompt_id": turn.get("prompt_id", ""),
        "response_latency_ms": turn.get("response_latency_ms"),
        "human_think_ms": turn.get("human_think_ms", 0) or 0,
        "downtime_ms": turn.get("downtime_ms", 0) or 0,
        "gap": turn.get("gap"),
        "gap_segments": turn.get("gap_segments", []),
        "focus_gap_ms": turn.get("focus_gap_ms", 0),
        "attention_idle_ms": turn.get("attention_idle_ms", 0),
        "off_hours_away_ms": turn.get("off_hours_away_ms", 0),
        "agent_runtime_ms": turn.get("agent_runtime_ms", 0),
        "work_session_break": bool(turn.get("work_session_break")),
        "turn_duration_ms": dur_ms if dur_ms > 0 else None,
        "steps": len(responses),
        "total_output_tokens": sum(r.get("output_tokens", 0) for r in responses),
        "total_input_tokens": sum(r.get("input_tokens", 0) for r in responses),
        "tools_used": turn.get("tools_used", []),
        "models": list(set(r.get("model", "") for r in responses if r.get("model"))),
        "stop_reason": responses[-1].get("stop_reason", ""),
        "hour": turn.get("hour"),
    }
    turns_list.append(turn_data)


def _apply_global_gap_classification(
    turns: list[dict],
    working_hours: WorkingHours,
    local_tz: tzinfo,
    *,
    autonomous: bool = False,
) -> None:
    """Attach classified gaps across the merged activity timeline."""
    if not turns:
        return

    for turn in turns:
        turn["gap"] = None
        turn["gap_segments"] = []
        turn["focus_gap_ms"] = 0
        turn["attention_idle_ms"] = 0
        turn["off_hours_away_ms"] = 0
        turn["agent_runtime_ms"] = 0
        turn["work_session_break"] = False
        turn["human_think_ms"] = 0
        turn["downtime_ms"] = 0
        turn.pop("human_think_start_ts", None)
        turn.pop("human_think_end_ts", None)
        turn.pop("downtime_start_ts", None)
        turn.pop("downtime_end_ts", None)

    sorted_turns = sorted(
        (t for t in turns if t.get("human_ts")),
        key=lambda t: t["human_ts"],
    )
    if not sorted_turns:
        return

    active_end = _turn_active_end(sorted_turns[0])
    for turn in sorted_turns[1:]:
        start = turn["human_ts"]
        end = _turn_active_end(turn)
        if start > active_end:
            _attach_classified_gap(turn, active_end, start, working_hours, local_tz, autonomous=autonomous)
        if end > active_end:
            active_end = end


def _turn_active_end(turn: dict) -> datetime:
    human_ts = turn.get("human_ts")
    last_agent_ts = turn.get("last_agent_ts") or human_ts
    return max(human_ts, last_agent_ts)


def _attach_classified_gap(
    turn: dict,
    gap_start: datetime,
    gap_end: datetime,
    working_hours: WorkingHours,
    local_tz: tzinfo,
    *,
    autonomous: bool = False,
) -> None:
    gap = _classify_gap(gap_start, gap_end, working_hours, local_tz=local_tz, autonomous=autonomous)
    turn["gap"] = gap
    turn["gap_segments"] = gap["segments"]
    turn["focus_gap_ms"] = gap["focus_ms"]
    turn["attention_idle_ms"] = gap["attention_idle_ms"]
    turn["off_hours_away_ms"] = gap["off_hours_ms"]
    turn["agent_runtime_ms"] = gap["agent_runtime_ms"]
    turn["work_session_break"] = gap["work_session_break"]
    turn["human_think_ms"] = gap["focus_ms"] + gap["attention_idle_ms"]
    turn["downtime_ms"] = 0


def _classify_gap(
    gap_start: datetime,
    gap_end: datetime,
    working_hours: WorkingHours,
    *,
    local_tz: tzinfo,
    autonomous: bool = False,
) -> GapClassification:
    gap_start = _ensure_tz(gap_start, local_tz)
    gap_end = _ensure_tz(gap_end, local_tz)
    gap_ms = round((gap_end - gap_start).total_seconds() * 1000)
    if gap_ms <= 0:
        return _empty_gap_classification()

    if autonomous:
        return _classify_autonomous_gap(gap_start, gap_end, gap_ms)

    work_session_break = gap_ms > WORK_SESSION_BREAK_MS
    working_state = "focus_gap" if gap_ms <= FOCUS_GAP_MS else "attention_idle"
    break_at = gap_start + timedelta(milliseconds=WORK_SESSION_BREAK_MS) if work_session_break else gap_end
    focus_ms = 0
    attention_idle_ms = 0
    off_hours_ms = 0
    state_totals: dict[str, int] = defaultdict(int)
    segments: list[GapSegment] = []

    cursor = gap_start
    while cursor < gap_end:
        chunk_end = _next_gap_boundary(cursor, gap_end, local_tz, break_at if work_session_break else None)
        if chunk_end <= cursor:
            break
        local_cursor = cursor.astimezone(local_tz)
        state = working_state if _is_working_or_fringe(local_cursor, working_hours) else "off_hours_away"
        ms = round((chunk_end - cursor).total_seconds() * 1000)
        session_attributed = not work_session_break or chunk_end <= break_at
        segment: GapSegment = {
            "start": cursor,
            "end": chunk_end,
            "state": state,
            "ms": ms,
            "session_attributed": session_attributed,
        }
        segments.append(segment)
        state_totals[state] += ms
        if state == "off_hours_away":
            off_hours_ms += ms
        elif session_attributed and state == "focus_gap":
            focus_ms += ms
        elif session_attributed and state == "attention_idle":
            attention_idle_ms += ms
        cursor = chunk_end

    return {
        "state": _dominant_gap_state(state_totals),
        "focus_ms": focus_ms,
        "attention_idle_ms": attention_idle_ms,
        "off_hours_ms": off_hours_ms,
        "agent_runtime_ms": 0,
        "work_session_break": work_session_break,
        "segments": segments,
    }


def _classify_autonomous_gap(gap_start: datetime, gap_end: datetime, gap_ms: int) -> GapClassification:
    work_session_break = gap_ms > AUTONOMOUS_IDLE_BREAK_MS
    attributed_ms = min(gap_ms, AUTONOMOUS_IDLE_BREAK_MS) if work_session_break else gap_ms
    segments: list[GapSegment] = []
    state_totals: dict[str, int] = defaultdict(int)
    if attributed_ms > 0:
        attributed_end = gap_start + timedelta(milliseconds=attributed_ms)
        segment: GapSegment = {
            "start": gap_start,
            "end": min(attributed_end, gap_end),
            "state": "agent_runtime",
            "ms": attributed_ms,
            "session_attributed": True,
        }
        segments.append(segment)
        state_totals["agent_runtime"] += attributed_ms
    if work_session_break:
        rest_start = gap_start + timedelta(milliseconds=attributed_ms)
        if rest_start < gap_end:
            rest_ms = round((gap_end - rest_start).total_seconds() * 1000)
            segment = {
                "start": rest_start,
                "end": gap_end,
                "state": "agent_runtime",
                "ms": rest_ms,
                "session_attributed": False,
            }
            segments.append(segment)
            state_totals["agent_runtime"] += rest_ms

    return {
        "state": _dominant_gap_state(state_totals),
        "focus_ms": 0,
        "attention_idle_ms": 0,
        "off_hours_ms": 0,
        "agent_runtime_ms": attributed_ms,
        "work_session_break": work_session_break,
        "segments": segments,
    }


def _empty_gap_classification() -> GapClassification:
    return {
        "state": "none",
        "focus_ms": 0,
        "attention_idle_ms": 0,
        "off_hours_ms": 0,
        "agent_runtime_ms": 0,
        "work_session_break": False,
        "segments": [],
    }


def _ensure_tz(value: datetime, local_tz: tzinfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=local_tz)
    return value


def _next_gap_boundary(
    cursor: datetime,
    gap_end: datetime,
    local_tz: tzinfo,
    break_at: datetime | None,
) -> datetime:
    local_cursor = cursor.astimezone(local_tz)
    next_hour = (local_cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    boundary = next_hour.astimezone(cursor.tzinfo or timezone.utc)
    if boundary <= cursor:
        boundary = cursor + timedelta(hours=1)
    if break_at and cursor < break_at < boundary:
        boundary = break_at
    return min(gap_end, boundary)


def _is_working_or_fringe(local_dt: datetime, working_hours: WorkingHours) -> bool:
    per_weekday = working_hours.get("per_weekday") if isinstance(working_hours, dict) else None
    if not isinstance(per_weekday, dict):
        return False
    key = WEEKDAY_KEYS[local_dt.weekday()]
    entry = per_weekday.get(key)
    if not isinstance(entry, dict):
        return False
    hour = local_dt.hour
    return _hour_in_window(hour, entry.get("working")) or _hour_in_window(hour, entry.get("fringe"))


def _hour_in_window(hour: int, window: object) -> bool:
    if not isinstance(window, list) or len(window) != 2:
        return False
    start, end = window
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return start <= hour < end


def _dominant_gap_state(state_totals: dict[str, int]) -> str:
    if not state_totals:
        return "none"
    priority = {
        "agent_runtime": 0,
        "attention_idle": 1,
        "focus_gap": 2,
        "off_hours_away": 3,
    }
    return max(
        state_totals.items(),
        key=lambda item: (item[1], -priority.get(item[0], 99)),
    )[0]


def _gap_state_bucket_field(state: str) -> str | None:
    return {
        "focus_gap": "focus_gap_sec",
        "attention_idle": "attention_idle_sec",
        "off_hours_away": "off_hours_away_sec",
        "agent_runtime": "agent_runtime_sec",
    }.get(state)


def _add_gap_segments_to_daily(daily_stats: dict[str, dict], turn: dict, local_tz: tzinfo) -> None:
    idle_days: set[str] = set()
    for segment in turn.get("gap_segments") or []:
        if not isinstance(segment, dict):
            continue
        state = str(segment.get("state") or "")
        stat_field = _gap_state_bucket_field(state)
        if not stat_field:
            continue
        start = segment.get("start")
        end = segment.get("end")
        if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
            continue
        cursor = start
        while cursor < end:
            local_cursor = cursor.astimezone(local_tz)
            next_day = (local_cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            boundary = next_day.astimezone(cursor.tzinfo or timezone.utc)
            chunk_end = min(end, boundary)
            if chunk_end <= cursor:
                chunk_end = min(end, cursor + timedelta(hours=1))
            day = local_cursor.strftime("%Y-%m-%d")
            daily_stats[day][stat_field] += (chunk_end - cursor).total_seconds()
            if state == "attention_idle":
                idle_days.add(day)
            cursor = chunk_end
    # Count one downtime event per logical gap per calendar day.
    for day in idle_days:
        daily_stats[day]["downtime_events"] += 1


def _session_source_metadata(path: str, cwd: str | None = None) -> dict:
    path_obj = Path(path)
    try:
        file_size = path_obj.stat().st_size
    except OSError:
        file_size = 0

    project_path = cwd or _decode_claude_project_path(path_obj)
    project_key = _project_key_from_path(project_path)
    repo_path = _repo_path_for_project(project_path, project_key)
    return {
        "project": project_key or "unknown",
        "project_path": str(project_path) if project_path else None,
        "repo": Path(repo_path).name if repo_path else None,
        "repo_path": str(repo_path) if repo_path else None,
        "file_size_bytes": file_size,
        "classification": _session_classification(path_obj, file_size),
    }


def _decode_claude_project_path(path: Path) -> str | None:
    parts = list(path.parts)
    try:
        idx = parts.index("projects")
    except ValueError:
        project_dir = path.parent.name
    else:
        if idx == 0 or idx + 1 >= len(parts) or parts[idx - 1] != ".claude":
            project_dir = path.parent.name
        else:
            project_dir = parts[idx + 1]
    if not project_dir.startswith("-"):
        return None
    decoded = "/" + project_dir.strip("-").replace("-", "/")
    return decoded or None


def _session_classification(path: Path, file_size: int) -> str:
    if "subagents" in path.parts or path.name.startswith("agent-"):
        return "automated_subagent"
    if file_size and file_size < TRIVIAL_SESSION_BYTES:
        return "automated_trivial"
    return "interactive"


def _project_key_from_path(path: object) -> str | None:
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    lowered = text.lower().replace("_", "-")
    for known in KNOWN_PROJECT_REPOS:
        if known in lowered:
            return _canonical_project_key(known)

    parts = [part for part in Path(text).parts if part not in ("/", "")]
    lowered_parts = [part.lower().replace("_", "-") for part in parts]
    if "dev_projects" in lowered_parts:
        idx = lowered_parts.index("dev_projects")
        if idx + 1 < len(parts):
            return _canonical_project_key(parts[idx + 1].lower().replace("_", "-"))
    for idx in range(len(lowered_parts) - 1):
        if lowered_parts[idx] == "dev" and lowered_parts[idx + 1] == "projects":
            tail = [part.lower().replace("_", "-") for part in parts[idx + 2:]]
            return _canonical_project_key("-".join(tail)) if tail else None
    return _canonical_project_key(Path(text).name.lower().replace("_", "-")) or None


def _canonical_project_key(project: str | None) -> str | None:
    if not project:
        return None
    return PROJECT_ALIASES.get(project, project)


def _repo_path_for_project(project_path: object, project_key: str | None) -> str | None:
    if project_path:
        path = Path(str(project_path)).expanduser()
        repo = _find_git_root(path)
        if repo:
            return str(repo)
    if project_key:
        candidate = KNOWN_PROJECT_REPOS.get(project_key)
        if candidate and (candidate / ".git").exists():
            return str(candidate)
    return None


def _find_git_root(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except OSError:
        current = path
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None
