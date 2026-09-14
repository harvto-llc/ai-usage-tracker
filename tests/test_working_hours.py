import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src import working_hours as wh


LA = ZoneInfo("America/Los_Angeles")


def _local_dt(day: date, hour: int, tz=LA) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=tz)


def _daily_timestamps(start: date, days: int, hour: int, tz=LA) -> list[datetime]:
    return [_local_dt(start + timedelta(days=offset), hour, tz) for offset in range(days)]


def _weekday_9_to_5_timestamps(start: date, weeks: int = 2) -> list[datetime]:
    timestamps = []
    for offset in range(weeks * 7):
        day = start + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour in range(9, 17):
            timestamps.append(_local_dt(day, hour))
    return timestamps


def _assert_fallback(result: dict, sample_days: int) -> None:
    assert result["sample_days"] == sample_days
    for entry in result["per_weekday"].values():
        assert entry["working"] == [8, 22]
        assert entry["fringe"] == [7, 23]
        assert entry["peak_hour"] is None


def _use_temp_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(wh, "WORKING_HOURS_CACHE", tmp_path / "working_hours.json")


def test_weekday_9_to_5_pattern_infers_weekdays_and_empty_weekends():
    timestamps = _weekday_9_to_5_timestamps(date(2026, 3, 2), weeks=2)
    result = wh.infer_working_hours(timestamps, _local_dt(date(2026, 3, 20), 12), LA)

    assert result["sample_days"] == 10
    for key in ("mon", "tue", "wed", "thu", "fri"):
        assert result["per_weekday"][key]["working"] == [9, 17]
        assert result["per_weekday"][key]["fringe"] == [8, 18]
        assert result["per_weekday"][key]["peak_hour"] == 9
    for key in ("sat", "sun"):
        assert result["per_weekday"][key] == {"working": [], "fringe": [], "peak_hour": None}


def test_zero_turns_fall_back_to_default_window():
    result = wh.infer_working_hours([], _local_dt(date(2026, 4, 1), 12), LA)

    _assert_fallback(result, sample_days=0)


def test_three_sample_days_fall_back_to_default_window():
    timestamps = _daily_timestamps(date(2026, 4, 1), 3, 11)
    result = wh.infer_working_hours(timestamps, _local_dt(date(2026, 4, 4), 12), LA)

    _assert_fallback(result, sample_days=3)


def test_thirty_sample_days_infer_observed_hour():
    timestamps = _daily_timestamps(date(2026, 1, 1), 30, 10)
    result = wh.infer_working_hours(timestamps, _local_dt(date(2026, 2, 1), 12), LA)

    assert result["sample_days"] == 30
    for entry in result["per_weekday"].values():
        assert entry["working"] == [10, 11]
        assert entry["fringe"] == [9, 12]
        assert entry["peak_hour"] == 10


def test_dst_transition_buckets_by_local_hour_not_utc_hour():
    local_timestamps = _daily_timestamps(date(2026, 3, 4), 10, 9)
    utc_timestamps = [timestamp.astimezone(timezone.utc) for timestamp in local_timestamps]

    result = wh.infer_working_hours(utc_timestamps, _local_dt(date(2026, 3, 15), 12), LA)

    assert result["sample_days"] == 10
    for entry in result["per_weekday"].values():
        assert entry["working"] == [9, 10]
        assert entry["peak_hour"] == 9


def test_cache_hit_returns_fresh_cached_working_hours(monkeypatch, tmp_path):
    _use_temp_cache(monkeypatch, tmp_path)
    now = _local_dt(date(2026, 4, 1), 12)
    first = wh.load_or_infer_working_hours(_daily_timestamps(date(2026, 2, 1), 30, 10), now, LA)
    second = wh.load_or_infer_working_hours(
        _daily_timestamps(date(2026, 2, 1), 30, 14),
        now + timedelta(hours=1),
        LA,
    )

    assert wh.WORKING_HOURS_CACHE.exists()
    assert second == first
    assert second["per_weekday"]["mon"]["working"] == [10, 11]


def test_force_recomputes_fresh_cache_and_records_offset(monkeypatch, tmp_path):
    _use_temp_cache(monkeypatch, tmp_path)
    now = _local_dt(date(2026, 4, 1), 12)
    first = wh.load_or_infer_working_hours(_daily_timestamps(date(2026, 2, 1), 30, 10), now, LA)
    forced = wh.load_or_infer_working_hours(
        _daily_timestamps(date(2026, 2, 1), 30, 14),
        now + timedelta(hours=1),
        LA,
        force=True,
    )
    cache_record = json.loads(wh.WORKING_HOURS_CACHE.read_text())

    assert forced != first
    assert forced["per_weekday"]["mon"]["working"] == [14, 15]
    assert forced["computed_at"].endswith("-07:00")
    assert cache_record["timezone"] == "America/Los_Angeles"
    assert cache_record["working_hours"]["computed_at"] == forced["computed_at"]


def test_cache_expires_after_ttl(monkeypatch, tmp_path):
    _use_temp_cache(monkeypatch, tmp_path)
    now = _local_dt(date(2026, 4, 1), 12)
    first = wh.load_or_infer_working_hours([], now, LA)
    second = wh.load_or_infer_working_hours(
        _daily_timestamps(date(2026, 2, 1), 30, 13),
        now + wh.CACHE_TTL + timedelta(minutes=1),
        LA,
    )

    assert first["sample_days"] == 0
    assert second["sample_days"] == 30
    assert second["per_weekday"]["mon"]["working"] == [13, 14]


def test_cache_invalidates_when_timezone_changes(monkeypatch, tmp_path):
    _use_temp_cache(monkeypatch, tmp_path)
    now = _local_dt(date(2026, 4, 1), 12)
    local_timestamps = _daily_timestamps(date(2026, 2, 25), 30, 9)
    utc_timestamps = [timestamp.astimezone(timezone.utc) for timestamp in local_timestamps]
    first = wh.load_or_infer_working_hours(utc_timestamps, now, LA)
    second = wh.load_or_infer_working_hours(
        utc_timestamps,
        now.astimezone(timezone.utc) + timedelta(hours=1),
        timezone.utc,
    )

    assert first["per_weekday"]["mon"]["working"] == [9, 10]
    assert second["per_weekday"]["mon"]["working"] != [9, 10]


def test_cache_refreshes_when_newer_human_timestamp_arrives(monkeypatch, tmp_path):
    _use_temp_cache(monkeypatch, tmp_path)
    now = _local_dt(date(2026, 4, 1), 12)
    old_timestamps = _daily_timestamps(date(2026, 1, 1), 30, 10)
    first = wh.load_or_infer_working_hours(old_timestamps, now, LA)

    newer_human_ts = now + timedelta(minutes=30)
    refreshed = wh.load_or_infer_working_hours(
        [*old_timestamps, newer_human_ts],
        now + timedelta(hours=1),
        LA,
    )

    assert refreshed["computed_at"] != first["computed_at"]
    assert refreshed["computed_at"] == (now + timedelta(hours=1)).isoformat()
    assert refreshed["sample_days"] == 31
