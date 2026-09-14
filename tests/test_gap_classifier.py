from datetime import datetime, timedelta, timezone

from src.gap_rollups import (
    AUTONOMOUS_IDLE_BREAK_MS,
    FOCUS_GAP_MS,
    WORK_SESSION_BREAK_MS,
    _classify_gap,
)


def _dt(hour: int, minute: int = 0, *, day: int = 20) -> datetime:
    return datetime(2026, 4, day, hour, minute, tzinfo=timezone.utc)


def _working_hours(*, working=None, fringe=None, empty_weekends: bool = False):
    if working is None:
        working = [9, 17]
    if fringe is None:
        fringe = []
    per_weekday = {}
    for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        if empty_weekends and key in {"sat", "sun"}:
            per_weekday[key] = {"working": [], "fringe": [], "peak_hour": None}
        else:
            per_weekday[key] = {
                "working": list(working),
                "fringe": list(fringe),
                "peak_hour": working[0] if working else None,
            }
    return {
        "computed_at": "2026-04-21T00:00:00+00:00",
        "sample_days": 30,
        "per_weekday": per_weekday,
    }


def _segment_ms(gap: dict, state: str, *, attributed: bool | None = None) -> int:
    total = 0
    for segment in gap["segments"]:
        if segment["state"] != state:
            continue
        if attributed is not None and segment["session_attributed"] is not attributed:
            continue
        total += segment["ms"]
    return total


def test_overnight_gap_is_off_hours_not_idle():
    gap = _classify_gap(
        _dt(23),
        _dt(9, day=21),
        _working_hours(working=[9, 17], fringe=[]),
        local_tz=timezone.utc,
    )

    assert gap["off_hours_ms"] == 10 * 60 * 60 * 1000
    assert gap["attention_idle_ms"] == 0
    assert gap["focus_ms"] == 0
    assert gap["work_session_break"] is True
    assert gap["state"] == "off_hours_away"


def test_four_minute_working_gap_is_focus():
    gap = _classify_gap(
        _dt(14),
        _dt(14) + timedelta(minutes=4),
        _working_hours(),
        local_tz=timezone.utc,
    )

    assert gap["focus_ms"] == 4 * 60 * 1000
    assert gap["attention_idle_ms"] == 0
    assert gap["work_session_break"] is False
    assert gap["state"] == "focus_gap"


def test_twelve_minute_working_gap_is_all_attention_idle():
    gap = _classify_gap(
        _dt(14),
        _dt(14) + timedelta(minutes=12),
        _working_hours(),
        local_tz=timezone.utc,
    )

    assert gap["focus_ms"] == 0
    assert gap["attention_idle_ms"] == 12 * 60 * 1000
    assert gap["work_session_break"] is False
    assert gap["state"] == "attention_idle"


def test_fifty_minute_working_gap_caps_session_attributed_attention():
    gap = _classify_gap(
        _dt(14),
        _dt(14) + timedelta(minutes=50),
        _working_hours(),
        local_tz=timezone.utc,
    )

    assert gap["focus_ms"] == 0
    assert gap["attention_idle_ms"] == WORK_SESSION_BREAK_MS
    assert gap["work_session_break"] is True
    assert _segment_ms(gap, "attention_idle", attributed=False) == 5 * 60 * 1000


def test_evening_gap_keeps_post_break_segments_for_day_buckets():
    gap = _classify_gap(
        _dt(18),
        _dt(23),
        _working_hours(working=[9, 22], fringe=[]),
        local_tz=timezone.utc,
    )

    assert gap["attention_idle_ms"] == WORK_SESSION_BREAK_MS
    assert gap["focus_ms"] == 0
    assert gap["off_hours_ms"] == 60 * 60 * 1000
    assert gap["work_session_break"] is True
    assert _segment_ms(gap, "attention_idle", attributed=False) == 195 * 60 * 1000
    assert _segment_ms(gap, "off_hours_away", attributed=False) == 60 * 60 * 1000


def test_fringe_counts_as_working_time():
    gap = _classify_gap(
        _dt(22, 30),
        _dt(22, 45),
        _working_hours(working=[9, 22], fringe=[8, 23]),
        local_tz=timezone.utc,
    )

    assert gap["attention_idle_ms"] == 15 * 60 * 1000
    assert gap["focus_ms"] == 0
    assert gap["off_hours_ms"] == 0
    assert gap["work_session_break"] is False


def test_weekend_empty_windows_are_off_hours():
    gap = _classify_gap(
        _dt(15, day=18),
        _dt(16, day=18),
        _working_hours(empty_weekends=True),
        local_tz=timezone.utc,
    )

    assert gap["off_hours_ms"] == 60 * 60 * 1000
    assert gap["attention_idle_ms"] == 0
    assert gap["focus_ms"] == 0
    assert gap["work_session_break"] is True


def test_autonomous_gap_below_break_is_agent_runtime():
    gap = _classify_gap(
        _dt(14),
        _dt(14) + timedelta(minutes=25),
        _working_hours(),
        local_tz=timezone.utc,
        autonomous=True,
    )

    assert gap["agent_runtime_ms"] == 25 * 60 * 1000
    assert gap["work_session_break"] is False
    assert gap["state"] == "agent_runtime"


def test_autonomous_gap_above_break_caps_session_attributed_runtime():
    gap = _classify_gap(
        _dt(14),
        _dt(14) + timedelta(minutes=35),
        _working_hours(),
        local_tz=timezone.utc,
        autonomous=True,
    )

    assert gap["agent_runtime_ms"] == AUTONOMOUS_IDLE_BREAK_MS
    assert gap["work_session_break"] is True
    assert _segment_ms(gap, "agent_runtime", attributed=True) == AUTONOMOUS_IDLE_BREAK_MS
    assert _segment_ms(gap, "agent_runtime", attributed=False) == 5 * 60 * 1000


def test_non_positive_gap_returns_zero_buckets():
    gap = _classify_gap(_dt(14), _dt(14), _working_hours(), local_tz=timezone.utc)

    assert gap == {
        "state": "none",
        "focus_ms": 0,
        "attention_idle_ms": 0,
        "off_hours_ms": 0,
        "agent_runtime_ms": 0,
        "work_session_break": False,
        "segments": [],
    }
