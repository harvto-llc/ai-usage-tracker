"""Infer local working-hour windows from human turn timestamps."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import NotRequired, TypedDict

WORKING_HOUR_DENSITY_THRESHOLD = 0.20
MIN_SAMPLE_DAYS = 7
FALLBACK_WORKING_WINDOW = [8, 22]
CACHE_TTL = timedelta(hours=24)
WORKING_HOURS_CACHE = Path.home() / ".usage-tracker" / "working_hours.json"

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WeekdayWorkingHours(TypedDict):
    working: list[int]
    fringe: list[int]
    peak_hour: int | None


class WorkingHours(TypedDict):
    computed_at: str
    sample_days: int
    per_weekday: dict[str, WeekdayWorkingHours]


class WorkingHoursCacheRecord(TypedDict):
    timezone: str
    working_hours: WorkingHours
    latest_human_ts: NotRequired[str | None]


def infer_working_hours(
    human_timestamps: Iterable[object],
    now: datetime,
    tz: tzinfo,
) -> WorkingHours:
    """Infer per-weekday working windows from human timestamps."""
    local_now = _localize_timestamp(now, tz)
    local_timestamps = [
        local_ts
        for timestamp in human_timestamps
        if (local_ts := _coerce_local_timestamp(timestamp, tz)) is not None
    ]
    sample_days = len({timestamp.date() for timestamp in local_timestamps})

    if sample_days < MIN_SAMPLE_DAYS:
        return {
            "computed_at": local_now.isoformat(),
            "sample_days": sample_days,
            "per_weekday": _fallback_weekdays(),
        }

    histograms = _build_weekday_histograms(local_timestamps)
    per_weekday: dict[str, WeekdayWorkingHours] = {}
    for index, key in enumerate(WEEKDAY_KEYS):
        per_weekday[key] = _weekday_entry_from_histogram(histograms[index])

    return {
        "computed_at": local_now.isoformat(),
        "sample_days": sample_days,
        "per_weekday": per_weekday,
    }


def load_or_infer_working_hours(
    human_timestamps: Iterable[object],
    now: datetime,
    tz: tzinfo,
    force: bool = False,
) -> WorkingHours:
    """Load a fresh cache entry or recompute working hours."""
    local_now = _localize_timestamp(now, tz)
    local_timestamps = [
        local_ts
        for timestamp in human_timestamps
        if (local_ts := _coerce_local_timestamp(timestamp, tz)) is not None
    ]
    latest_human_ts = max(local_timestamps, default=None)

    if not force:
        cached = _read_cache()
        if cached and _cache_is_valid(cached, local_now, tz, latest_human_ts):
            return cached["working_hours"]

    working_hours = infer_working_hours(local_timestamps, local_now, tz)
    _write_cache(working_hours, tz, latest_human_ts)
    return working_hours


def _coerce_local_timestamp(value: object, tz: tzinfo) -> datetime | None:
    if isinstance(value, datetime):
        return _localize_timestamp(value, tz)
    if isinstance(value, str):
        try:
            return _localize_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")), tz)
        except ValueError:
            return None
    return None


def _localize_timestamp(timestamp: datetime, tz: tzinfo) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=tz)
    return timestamp.astimezone(tz)


def _build_weekday_histograms(timestamps: list[datetime]) -> dict[int, list[int]]:
    histograms = {weekday: [0] * 24 for weekday in range(7)}
    for timestamp in timestamps:
        histograms[timestamp.weekday()][timestamp.hour] += 1
    return histograms


def _weekday_entry_from_histogram(histogram: list[int]) -> WeekdayWorkingHours:
    peak_count = max(histogram) if histogram else 0
    if peak_count <= 0:
        return {"working": [], "fringe": [], "peak_hour": None}

    peak_hour = histogram.index(peak_count)
    threshold = peak_count * WORKING_HOUR_DENSITY_THRESHOLD
    selected_hours = [hour for hour, count in enumerate(histogram) if count >= threshold and count > 0]
    if not selected_hours:
        return {"working": [], "fringe": [], "peak_hour": peak_hour}

    start = min(selected_hours)
    end = max(selected_hours) + 1
    return {
        "working": [start, end],
        "fringe": _fringe_window(start, end),
        "peak_hour": peak_hour,
    }


def _fallback_weekdays() -> dict[str, WeekdayWorkingHours]:
    start, end = FALLBACK_WORKING_WINDOW
    return {
        key: {
            "working": list(FALLBACK_WORKING_WINDOW),
            "fringe": _fringe_window(start, end),
            "peak_hour": None,
        }
        for key in WEEKDAY_KEYS
    }


def _fringe_window(start: int, end: int) -> list[int]:
    return [max(0, start - 1), min(24, end + 1)]


def _read_cache() -> WorkingHoursCacheRecord | None:
    try:
        raw = json.loads(WORKING_HOURS_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    working_hours = raw.get("working_hours")
    if not _valid_working_hours(working_hours):
        return None

    timezone_name = raw.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        return None

    latest_human_ts = raw.get("latest_human_ts")
    if latest_human_ts is not None and not isinstance(latest_human_ts, str):
        latest_human_ts = None

    return {
        "timezone": timezone_name,
        "working_hours": working_hours,
        "latest_human_ts": latest_human_ts,
    }


def _write_cache(working_hours: WorkingHours, tz: tzinfo, latest_human_ts: datetime | None) -> None:
    record: WorkingHoursCacheRecord = {
        "timezone": _timezone_key(tz, _parse_datetime(working_hours["computed_at"])),
        "working_hours": working_hours,
        "latest_human_ts": latest_human_ts.isoformat() if latest_human_ts else None,
    }
    try:
        WORKING_HOURS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = WORKING_HOURS_CACHE.with_name(f"{WORKING_HOURS_CACHE.name}.tmp")
        tmp_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        tmp_path.replace(WORKING_HOURS_CACHE)
    except OSError:
        return


def _cache_is_valid(
    record: WorkingHoursCacheRecord,
    now: datetime,
    tz: tzinfo,
    latest_human_ts: datetime | None,
) -> bool:
    working_hours = record["working_hours"]
    computed_at = _parse_datetime(working_hours.get("computed_at"))
    if computed_at is None:
        return False
    computed_at = computed_at.astimezone(tz)

    if record.get("timezone") != _timezone_key(tz, now):
        return False

    age = now - computed_at
    if age < timedelta(0) or age >= CACHE_TTL:
        return False

    if latest_human_ts and latest_human_ts > computed_at:
        return False

    return True


def _valid_working_hours(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("computed_at"), str):
        return False
    if not isinstance(value.get("sample_days"), int):
        return False
    per_weekday = value.get("per_weekday")
    if not isinstance(per_weekday, dict):
        return False
    return all(_valid_weekday_entry(per_weekday.get(key)) for key in WEEKDAY_KEYS)


def _valid_weekday_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    working = value.get("working")
    fringe = value.get("fringe")
    peak_hour = value.get("peak_hour")
    return (
        _valid_window(working)
        and _valid_window(fringe)
        and (peak_hour is None or (isinstance(peak_hour, int) and 0 <= peak_hour <= 23))
    )


def _valid_window(value: object) -> bool:
    if not isinstance(value, list) or len(value) not in {0, 2}:
        return False
    if not all(isinstance(hour, int) and 0 <= hour <= 24 for hour in value):
        return False
    return not value or value[0] <= value[1]


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _timezone_key(tz: tzinfo, reference: datetime | None) -> str:
    zone_key = getattr(tz, "key", None)
    if isinstance(zone_key, str) and zone_key:
        return zone_key
    if reference is not None:
        tz_name = tz.tzname(reference)
        if tz_name:
            return tz_name
    return str(tz)
