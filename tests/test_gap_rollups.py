import json
from datetime import datetime, timezone

import src.gap_rollups as gap_rollups_module
from src.gap_rollups import (
    _analyze_session,
    _apply_global_gap_classification,
    _normalize_period_days,
    _period_label,
    _session_source_metadata,
    gap_rollups_for_stats,
)


def _working_hours(window=None, fringe=None):
    if window is None:
        window = [0, 24]
    if fringe is None:
        fringe = []
    return {
        "computed_at": "2026-04-21T00:00:00+00:00",
        "sample_days": 30,
        "per_weekday": {
            key: {"working": list(window), "fringe": list(fringe), "peak_hour": window[0] if window else None}
            for key in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
    }


def _off_hours():
    return _working_hours(window=[], fringe=[])


def _write_jsonl(path, entries):
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")


def test_period_helpers_support_all_time():
    assert _normalize_period_days("30") == 30
    assert _normalize_period_days(0) == 0
    assert _normalize_period_days(-5) == 0
    assert _normalize_period_days("bad") == 7
    assert _period_label(7) == "7d"
    assert _period_label(0) == "all-time"


def _user(ts: str, prompt_id: str, session_id: str = "session-1", cwd: str | None = None) -> dict:
    entry = {
        "timestamp": ts,
        "type": "user",
        "sessionId": session_id,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "prompt"},
    }
    if cwd:
        entry["cwd"] = cwd
    return entry


def _assistant(
    ts: str,
    prompt_id: str,
    tokens: int = 10,
    session_id: str = "session-1",
    cwd: str | None = None,
) -> dict:
    entry = {
        "timestamp": ts,
        "type": "assistant",
        "sessionId": session_id,
        "promptId": prompt_id,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [],
            "usage": {"input_tokens": 5, "output_tokens": tokens},
            "stop_reason": "end_turn",
        },
    }
    if cwd:
        entry["cwd"] = cwd
    return entry


def test_session_analytics_exposes_working_hours(tmp_path, monkeypatch):
    gap_rollups_module._cache.clear()
    gap_rollups_module._cache_ts.clear()
    claude_project_dir = tmp_path / ".claude/projects/-Users-amgad-dev-projects-usage-tracker"
    claude_project_dir.mkdir(parents=True)
    session_file = claude_project_dir / "session.jsonl"
    _write_jsonl(
        session_file,
        [
            _user("2026-04-19T10:00:00+00:00", "p1", cwd=str(tmp_path)),
            _assistant("2026-04-19T10:00:10+00:00", "p1", cwd=str(tmp_path)),
        ],
    )

    sentinel = {
        "computed_at": "2026-04-21T12:00:00-07:00",
        "sample_days": 1,
        "per_weekday": {"mon": {"working": [9, 17], "fringe": [8, 18], "peak_hour": 10}},
    }

    def fake_source_metadata(path, cwd=None):
        return {
            "project": "usage-tracker",
            "project_path": str(tmp_path),
            "repo": None,
            "repo_path": None,
            "file_size_bytes": 20_000,
            "classification": "interactive",
        }

    monkeypatch.setattr(gap_rollups_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gap_rollups_module, "_session_source_metadata", fake_source_metadata)
    monkeypatch.setattr(gap_rollups_module, "load_or_infer_working_hours", lambda timestamps, now, tz: sentinel)

    try:
        result = gap_rollups_module.session_analytics(0)

        assert result["working_hours"] == sentinel
        assert result["daily"]
        assert result["daily"][0]["date"] == "2026-04-19"
        assert result["sessions_analyzed"] == 1
    finally:
        gap_rollups_module._cache.clear()
        gap_rollups_module._cache_ts.clear()


def test_session_analytics_daily_rows_expose_gap_fields_with_legacy_compat(tmp_path, monkeypatch):
    gap_rollups_module._cache.clear()
    gap_rollups_module._cache_ts.clear()
    claude_project_dir = tmp_path / ".claude/projects/-Users-amgad-dev-projects-usage-tracker"
    claude_project_dir.mkdir(parents=True)
    session_file = claude_project_dir / "session.jsonl"
    _write_jsonl(
        session_file,
        [
            _user("2026-04-19T10:00:00+00:00", "p1", cwd=str(tmp_path)),
            _assistant("2026-04-19T10:00:10+00:00", "p1", cwd=str(tmp_path)),
            _user("2026-04-19T10:05:00+00:00", "p2", cwd=str(tmp_path)),
            _assistant("2026-04-19T10:05:20+00:00", "p2", cwd=str(tmp_path)),
            _user("2026-04-19T10:40:00+00:00", "p3", cwd=str(tmp_path)),
            _assistant("2026-04-19T10:40:15+00:00", "p3", cwd=str(tmp_path)),
        ],
    )

    def fake_source_metadata(path, cwd=None):
        return {
            "project": "usage-tracker",
            "project_path": str(tmp_path),
            "repo": None,
            "repo_path": None,
            "file_size_bytes": 20_000,
            "classification": "interactive",
        }

    monkeypatch.setattr(gap_rollups_module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gap_rollups_module, "_session_source_metadata", fake_source_metadata)
    monkeypatch.setattr(gap_rollups_module, "load_or_infer_working_hours", lambda timestamps, now, tz: _working_hours())

    try:
        result = gap_rollups_module.session_analytics(0)

        day = result["daily"][0]
        assert day["date"] == "2026-04-19"
        assert day["focus_gap_sec"] == 290
        assert day["attention_idle_sec"] == 2080
        assert day["off_hours_away_sec"] == 0
        assert day["agent_runtime_sec"] == 0
        assert day["human_time_sec"] == 2370
        assert day["downtime_sec"] == 0
        assert day["downtime_events"] == 0
    finally:
        gap_rollups_module._cache.clear()
        gap_rollups_module._cache_ts.clear()


def test_global_gap_classifier_wires_attention_idle_and_zero_downtime_compat(tmp_path):
    session_file = tmp_path / "session.jsonl"
    _write_jsonl(
        session_file,
        [
            _user("2026-04-19T10:00:00+00:00", "p1"),
            _assistant("2026-04-19T10:00:10+00:00", "p1"),
            _user("2026-04-19T10:05:00+00:00", "p2"),
            _assistant("2026-04-19T10:05:20+00:00", "p2"),
            _user("2026-04-19T10:40:00+00:00", "p3"),
            _assistant("2026-04-19T10:40:15+00:00", "p3"),
        ],
    )

    result = _analyze_session(str(session_file), datetime(2026, 4, 18, tzinfo=timezone.utc))

    assert result is not None
    turns = result["turns"]
    _apply_global_gap_classification(turns, _working_hours(), timezone.utc)
    assert turns[1]["gap"]["state"] == "focus_gap"
    assert turns[1]["focus_gap_ms"] == 290_000
    assert turns[1]["attention_idle_ms"] == 0
    assert turns[1]["human_think_ms"] == 290_000
    assert turns[1]["downtime_ms"] == 0
    assert "human_think_start_ts" not in turns[1]
    assert "human_think_end_ts" not in turns[1]
    assert "downtime_start_ts" not in turns[1]
    assert "downtime_end_ts" not in turns[1]
    assert turns[2]["gap"]["state"] == "attention_idle"
    assert turns[2]["focus_gap_ms"] == 0
    assert turns[2]["attention_idle_ms"] == 2_080_000
    assert turns[2]["human_think_ms"] == 2_080_000
    assert turns[2]["downtime_ms"] == 0


def test_global_gap_classifier_preserves_parallel_agent_guard(tmp_path):
    session_a = tmp_path / "session-a.jsonl"
    session_b = tmp_path / "session-b.jsonl"
    _write_jsonl(
        session_a,
        [
            _user("2026-04-19T10:00:00+00:00", "a1", "session-a"),
            _assistant("2026-04-19T10:00:10+00:00", "a1", session_id="session-a"),
            _user("2026-04-19T11:00:00+00:00", "a2", "session-a"),
            _assistant("2026-04-19T11:00:10+00:00", "a2", session_id="session-a"),
        ],
    )
    _write_jsonl(
        session_b,
        [
            _user("2026-04-19T10:20:00+00:00", "b1", "session-b"),
            _assistant("2026-04-19T10:50:00+00:00", "b1", session_id="session-b"),
        ],
    )

    cutoff = datetime(2026, 4, 18, tzinfo=timezone.utc)
    result_a = _analyze_session(str(session_a), cutoff)
    result_b = _analyze_session(str(session_b), cutoff)

    assert result_a is not None
    assert result_b is not None
    turns = result_a["turns"] + result_b["turns"]
    _apply_global_gap_classification(turns, _working_hours(), timezone.utc)
    turns_by_prompt = {turn["prompt_id"]: turn for turn in turns}

    # The hour-long gap in session A is not idle because session B was active.
    assert turns_by_prompt["a2"]["gap"]["state"] == "attention_idle"
    assert turns_by_prompt["a2"]["attention_idle_ms"] == 600_000
    assert turns_by_prompt["a2"]["work_session_break"] is False
    assert turns_by_prompt["a2"]["downtime_ms"] == 0


def test_global_gap_classifier_counts_between_session_files(tmp_path):
    session_a = tmp_path / "session-a.jsonl"
    session_b = tmp_path / "session-b.jsonl"
    _write_jsonl(
        session_a,
        [
            _user("2026-04-19T10:00:00+00:00", "a1", "session-a"),
            _assistant("2026-04-19T10:00:10+00:00", "a1", session_id="session-a"),
        ],
    )
    _write_jsonl(
        session_b,
        [
            _user("2026-04-19T11:00:00+00:00", "b1", "session-b"),
            _assistant("2026-04-19T11:00:10+00:00", "b1", session_id="session-b"),
        ],
    )

    cutoff = datetime(2026, 4, 18, tzinfo=timezone.utc)
    result_a = _analyze_session(str(session_a), cutoff)
    result_b = _analyze_session(str(session_b), cutoff)

    assert result_a is not None
    assert result_b is not None
    turns = result_a["turns"] + result_b["turns"]
    _apply_global_gap_classification(turns, _working_hours(), timezone.utc)
    turns_by_prompt = {turn["prompt_id"]: turn for turn in turns}

    assert turns_by_prompt["b1"]["gap"]["state"] == "attention_idle"
    assert turns_by_prompt["b1"]["focus_gap_ms"] == 0
    assert turns_by_prompt["b1"]["attention_idle_ms"] == 2_700_000
    assert turns_by_prompt["b1"]["human_think_ms"] == 2_700_000
    assert turns_by_prompt["b1"]["downtime_ms"] == 0
    assert turns_by_prompt["b1"]["work_session_break"] is True


def test_global_gap_classifier_marks_multiday_break_as_off_hours(tmp_path):
    session_a = tmp_path / "session-a.jsonl"
    session_b = tmp_path / "session-b.jsonl"
    _write_jsonl(
        session_a,
        [
            _user("2026-04-17T10:00:00+00:00", "a1", "session-a"),
            _assistant("2026-04-17T10:00:10+00:00", "a1", session_id="session-a"),
        ],
    )
    _write_jsonl(
        session_b,
        [
            _user("2026-04-19T10:00:00+00:00", "b1", "session-b"),
            _assistant("2026-04-19T10:00:10+00:00", "b1", session_id="session-b"),
        ],
    )

    cutoff = datetime(2026, 4, 16, tzinfo=timezone.utc)
    result_a = _analyze_session(str(session_a), cutoff)
    result_b = _analyze_session(str(session_b), cutoff)

    assert result_a is not None
    assert result_b is not None
    turns = result_a["turns"] + result_b["turns"]
    _apply_global_gap_classification(turns, _off_hours(), timezone.utc)
    turns_by_prompt = {turn["prompt_id"]: turn for turn in turns}

    expected_gap_ms = round(
        (
            turns_by_prompt["b1"]["human_ts"] - turns_by_prompt["a1"]["last_agent_ts"]
        ).total_seconds()
        * 1000
    )
    assert turns_by_prompt["b1"]["gap"]["state"] == "off_hours_away"
    assert turns_by_prompt["b1"]["off_hours_away_ms"] == expected_gap_ms
    assert turns_by_prompt["b1"]["focus_gap_ms"] == 0
    assert turns_by_prompt["b1"]["attention_idle_ms"] == 0
    assert turns_by_prompt["b1"]["work_session_break"] is True
    assert turns_by_prompt["b1"]["downtime_ms"] == 0


def test_analyze_session_adds_project_and_source_metadata(tmp_path):
    repo = tmp_path / "usage-tracker"
    repo.mkdir()
    session_file = repo / "session.jsonl"
    _write_jsonl(
        session_file,
        [
            _user("2026-04-19T10:00:00+00:00", "p1", cwd=str(repo)),
            _assistant("2026-04-19T10:00:10+00:00", "p1", cwd=str(repo)),
        ],
    )

    result = _analyze_session(str(session_file), datetime(2026, 4, 18, tzinfo=timezone.utc))

    assert result is not None
    summary = result["summary"]
    assert summary["provider"] == "claude"
    assert summary["project"] == "usage-tracker"
    assert summary["project_path"] == str(repo)
    assert summary["file_size_bytes"] > 0
    assert summary["classification"] == "automated_trivial"


def test_subagent_session_metadata_uses_parent_project_and_automated_classification(tmp_path):
    session_file = (
        tmp_path
        / ".claude/projects/-Users-amgad-dev-projects-ai-agent-tts/session-1/subagents/agent-a1.jsonl"
    )
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n" * 2000)

    metadata = _session_source_metadata(str(session_file))

    assert metadata["project"] == "ai-agent-tts"
    assert metadata["classification"] == "automated_subagent"


def test_gap_rollups_for_stats_shape_and_sums(monkeypatch):
    """gap_rollups_for_stats exposes 4-state rollups for today/yesterday/last_7d."""
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

    monkeypatch.setattr(gap_rollups_module, "session_analytics", lambda days=7: synthetic)

    rollups = gap_rollups_for_stats()

    assert rollups["today_date"] == today_key
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
    assert last_7d["focus_gap_sec"] == 2100
    assert last_7d["attention_idle_sec"] == 700
    assert last_7d["off_hours_away_sec"] == 7200
    assert last_7d["agent_runtime_sec"] == 1600
    assert last_7d["human_time_sec"] == 2800
    assert last_7d["downtime_sec"] == 0

    # The 1900 row is neither today nor yesterday, so yesterday stays zeroed.
    yesterday = rollups["yesterday"]
    assert yesterday["focus_gap_sec"] == 0
    assert yesterday["human_time_sec"] == 0
