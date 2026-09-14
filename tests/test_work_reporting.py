import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import usage_ledger, work_ledger, work_reporting


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    return db_path


def _at_us(value: datetime) -> int:
    return round(value.timestamp() * 1_000_000)


def _insert_usage(
    db_path,
    *,
    provider: str,
    offset: int,
    at: datetime,
    session_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int = 0,
    usage_breakdown: dict | None = None,
) -> None:
    usage_ledger.init()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day, session_id,
                   cwd, model, is_message, is_user, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_5m_tokens,
                   cache_write_1h_tokens, reasoning_tokens, request_id,
                   usage_breakdown_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, 0, 0, 0, ?, ?)""",
            (
                provider,
                f"{provider}-file",
                offset,
                f"/{provider}/session.jsonl",
                _at_us(at),
                at.date().isoformat(),
                session_id,
                "/repo",
                model,
                input_tokens,
                output_tokens,
                cache_tokens,
                f"request-{provider}-{offset}",
                json.dumps(usage_breakdown or {}),
            ),
        )


def test_report_attributes_exact_events_and_rolls_children_into_project(activity_db):
    now = datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    repository_id = work_ledger.upsert_repository(
        {
            "id": "repo-1",
            "display_name": "usage-tracker",
            "identity_kind": "common_dir",
            "common_dir": "/repo/.git",
            "enabled": True,
        },
        now=1,
    )
    project = work_ledger.create_work_item(
        kind="project",
        name="Usage Tracker",
        repository_id=repository_id,
        color_hex="#2FAF88",
        now=1,
    )
    task = work_ledger.create_work_item(
        kind="task",
        name="Project reporting",
        parent_id=project["id"],
        now=1,
    )
    project_start = now - timedelta(hours=6)
    task_start = now - timedelta(hours=5)
    stop_at = now - timedelta(hours=3)
    work_ledger.switch_active_work_item(project["id"], at_us=_at_us(project_start), now=1)
    work_ledger.switch_active_work_item(task["id"], at_us=_at_us(task_start), now=2)
    work_ledger.switch_active_work_item(None, at_us=_at_us(stop_at), now=3)

    claude_session = work_ledger.upsert_ai_session(
        {
            "provider": "claude",
            "provider_session_id": "claude-session",
            "repository_id": repository_id,
            "models": ["claude-fable-5"],
        },
        now=1,
    )
    work_ledger.upsert_ai_session(
        {
            "provider": "codex",
            "provider_session_id": "codex-session",
            "repository_id": repository_id,
            "models": ["gpt-5.6-terra"],
        },
        now=1,
    )
    _insert_usage(
        activity_db,
        provider="claude",
        offset=1,
        at=project_start + timedelta(minutes=30),
        session_id="claude-session",
        model="claude-fable-5",
        input_tokens=100,
        output_tokens=50,
        cache_tokens=10,
        usage_breakdown={
            "files": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "reasoning_tokens": 0,
                "event_count": 2,
            }
        },
    )
    _insert_usage(
        activity_db,
        provider="codex",
        offset=1,
        at=task_start + timedelta(minutes=30),
        session_id="codex-session",
        model="gpt-5.6-terra",
        input_tokens=175,
        output_tokens=100,
        cache_tokens=25,
    )
    _insert_usage(
        activity_db,
        provider="claude",
        offset=2,
        at=now - timedelta(hours=1),
        session_id="claude-session",
        model="claude-unpublished",
        input_tokens=50,
        output_tokens=10,
    )
    work_ledger.upsert_activity_event(
        {
            "event_key": "edit-1",
            "kind": "edit",
            "occurred_at_us": _at_us(project_start + timedelta(minutes=45)),
            "repository_id": repository_id,
            "ai_session_id": claude_session,
            "provider": "claude",
            "source": "claude",
            "metadata": {"tool_name": "Edit"},
        },
        now=1,
    )
    work_ledger.upsert_activity_event(
        {
            "event_key": "commit-1",
            "kind": "git_commit",
            "occurred_at_us": _at_us(task_start + timedelta(hours=1)),
            "repository_id": repository_id,
            "source": "git",
            "metadata": {
                "commit_sha": "abc",
                "files_changed": 2,
                "additions": 20,
                "deletions": 5,
            },
        },
        now=1,
    )

    report = work_reporting.build_report("week", now=now)

    assert report["totals"]["tracked_seconds"] == 10_800
    assert report["totals"]["active_seconds"] == 10_800
    assert report["totals"]["total_tokens"] == 520
    assert report["totals"]["requests"] == 3
    assert report["totals"]["activity_events"] == 2
    assert report["totals"]["git_commits"] == 1
    assert report["totals"]["pricing_coverage_pct"] == 88.5
    assert report["totals"]["unpriced_models"] == ["claude-unpublished"]

    project_rollup = report["projects"][0]
    assert project_rollup["id"] == project["id"]
    assert project_rollup["metrics"]["tracked_seconds"] == 10_800
    assert project_rollup["metrics"]["active_seconds"] == 10_800
    assert project_rollup["metrics"]["total_tokens"] == 520
    assert project_rollup["metrics"]["activity_events"] == 2
    assert project_rollup["metrics"]["usage_categories"][0]["id"] == "files"
    assert project_rollup["metrics"]["usage_categories"][0]["total_tokens"] == 160

    direct = {row["id"]: row for row in report["work_items"]}
    assert direct[project["id"]]["metrics"]["tracked_seconds"] == 3_600
    assert direct[project["id"]]["metrics"]["active_seconds"] == 3_600
    assert direct[project["id"]]["metrics"]["total_tokens"] == 220
    assert direct[task["id"]]["metrics"]["tracked_seconds"] == 7_200
    assert direct[task["id"]]["metrics"]["active_seconds"] == 7_200
    assert direct[task["id"]]["metrics"]["total_tokens"] == 300
    assert direct[task["id"]]["color_hex"] == "#2FAF88"
    assert report["unassigned"]["total_tokens"] == 0
    assert report["unassigned"]["pricing_coverage_pct"] is None

    repository = report["repositories"][0]
    assert repository["id"] == repository_id
    assert repository["metrics"]["total_tokens"] == 520
    assert repository["metrics"]["git_commits"] == 1
    providers = {row["id"]: row["metrics"] for row in report["providers"]}
    assert providers["claude"]["total_tokens"] == 220
    assert providers["codex"]["total_tokens"] == 300
    assert providers["codex"]["estimated_credits"] is not None
    serialized = json.dumps(report)
    for private_field in (
        "metadata",
        "commit_sha",
        "commit_subject",
        "tool_name",
        "source_cursor",
        "cwd",
    ):
        assert private_field not in serialized


def test_report_infers_repository_project_and_branch_work(activity_db):
    now = datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    repository_id = work_ledger.upsert_repository(
        {
            "id": "repo-1",
            "display_name": "usage-tracker",
            "identity_kind": "common_dir",
            "common_dir": "/repo/.git",
            "enabled": True,
        },
        now=1,
    )
    session_id = work_ledger.upsert_ai_session(
        {
            "provider": "codex",
            "provider_session_id": "codex-session",
            "repository_id": repository_id,
            "branch": "feat/smart-report",
            "models": ["gpt-5.6-terra"],
        },
        now=1,
    )
    _insert_usage(
        activity_db,
        provider="codex",
        offset=1,
        at=now - timedelta(hours=1),
        session_id="codex-session",
        model="gpt-5.6-terra",
        input_tokens=100,
        output_tokens=25,
    )
    work_ledger.upsert_activity_event(
        {
            "event_key": "commit-1",
            "kind": "git_commit",
            "occurred_at_us": _at_us(now - timedelta(minutes=45)),
            "repository_id": repository_id,
            "source": "git",
            "metadata": {
                "commit_sha": "abc",
                "files_changed": 3,
                "additions": 20,
                "deletions": 5,
                "merge_commit": True,
            },
        },
        now=1,
    )
    work_ledger.upsert_activity_event(
        {
            "event_key": "pr-42",
            "kind": "git_pull_request",
            "occurred_at_us": _at_us(now - timedelta(minutes=44)),
            "repository_id": repository_id,
            "ai_session_id": session_id,
            "provider": "codex",
            "source": "git",
            "metadata": {
                "commit_sha": "abc",
                "pull_request_number": 42,
                "pull_request_branch": "feat/smart-report",
                "merge_commit": True,
            },
        },
        now=1,
    )

    report = work_reporting.build_report("week", now=now)

    assert len(report["projects"]) == 1
    project = report["projects"][0]
    assert project["name"] == "usage-tracker"
    assert project["inferred"] is True
    assert project["attribution_source"] == "repository"
    assert project["metrics"]["total_tokens"] == 125
    assert project["metrics"]["tracked_seconds"] == 0
    assert project["metrics"]["active_seconds"] == 16 * 60
    assert project["metrics"]["git_commits"] == 1
    assert project["metrics"]["git_pull_requests"] == 1
    assert project["metrics"]["merge_commits"] == 1
    assert project["metrics"]["changed_lines"] == 25
    branch = next(row for row in report["work_items"] if row["kind"] == "branch")
    assert branch["name"] == "feat/smart-report"
    assert branch["parent_id"] == project["id"]
    assert branch["metrics"]["total_tokens"] == 125
    assert branch["metrics"]["active_seconds"] == 16 * 60
    assert branch["metrics"]["git_pull_requests"] == 1
    assert report["unassigned"]["total_tokens"] == 0


def test_unique_branch_name_matches_explicit_task(activity_db):
    now = datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    repository_id = work_ledger.upsert_repository(
        {
            "id": "repo-1",
            "display_name": "usage-tracker",
            "identity_kind": "common_dir",
            "common_dir": "/repo/.git",
            "enabled": True,
        },
        now=1,
    )
    project = work_ledger.create_work_item(
        kind="project", name="Usage Tracker", repository_id=repository_id, now=1
    )
    task = work_ledger.create_work_item(
        kind="task", name="Smart Work Attribution", parent_id=project["id"], now=1
    )
    work_ledger.upsert_ai_session(
        {
            "provider": "claude",
            "provider_session_id": "claude-session",
            "repository_id": repository_id,
            "branch": "feature/smart-work-attribution",
        },
        now=1,
    )
    _insert_usage(
        activity_db,
        provider="claude",
        offset=1,
        at=now - timedelta(hours=1),
        session_id="claude-session",
        model="claude-fable-5",
        input_tokens=100,
        output_tokens=25,
    )

    report = work_reporting.build_report("week", now=now)

    task_bucket = next(row for row in report["work_items"] if row["id"] == task["id"])
    assert task_bucket["metrics"]["total_tokens"] == 125
    assert task_bucket["inferred"] is True
    assert task_bucket["attribution_source"] == "branch_match"
    assert task_bucket["attribution_confidence"] == 0.9


def test_session_assignment_overrides_global_default_and_can_force_unassigned(activity_db):
    now = datetime(2026, 7, 25, 16, tzinfo=timezone.utc)
    default_project = work_ledger.create_work_item(
        kind="project", name="Default Project", now=1
    )
    session_project = work_ledger.create_work_item(
        kind="project", name="Session Project", now=1
    )
    session_id = work_ledger.upsert_ai_session(
        {"provider": "codex", "provider_session_id": "codex-session"},
        now=1,
    )
    work_ledger.switch_active_work_item(
        default_project["id"],
        at_us=_at_us(now - timedelta(hours=2)),
        now=1,
    )
    work_ledger.set_session_work_assignment(
        session_id,
        mode="work_item",
        work_item_id=session_project["id"],
        now=2,
    )
    _insert_usage(
        activity_db,
        provider="codex",
        offset=1,
        at=now - timedelta(hours=1),
        session_id="codex-session",
        model="gpt-5.6-terra",
        input_tokens=100,
        output_tokens=25,
    )

    assigned = work_reporting.build_report("week", now=now)
    token_projects = [
        row for row in assigned["projects"] if row["metrics"]["total_tokens"] > 0
    ]
    assert [row["name"] for row in token_projects] == ["Session Project"]
    assert token_projects[0]["attribution_source"] == "session"
    assert assigned["unassigned"]["total_tokens"] == 0

    work_ledger.set_session_work_assignment(session_id, mode="unassigned", now=3)
    unassigned = work_reporting.build_report("week", now=now)
    assert all(row["metrics"]["total_tokens"] == 0 for row in unassigned["projects"])
    assert unassigned["unassigned"]["total_tokens"] == 125


def test_report_window_rejects_unknown_period():
    with pytest.raises(ValueError, match="day, week, or month"):
        work_reporting.build_report("quarter")


def test_day_window_uses_supplied_local_midnight():
    local = timezone(timedelta(hours=-7))
    now = datetime(2026, 7, 25, 16, 30, tzinfo=local)

    start, end = work_reporting.report_window("day", now=now)

    assert start == datetime(2026, 7, 25, tzinfo=local)
    assert end == now


def test_interval_switch_boundary_belongs_to_new_tag():
    index = work_reporting._IntervalIndex([
        {"work_item_id": "project", "started_at_us": 100, "ended_at_us": 200},
        {"work_item_id": "task", "started_at_us": 200, "ended_at_us": None},
    ])

    assert index.work_item_id_at(199) == "project"
    assert index.work_item_id_at(200) == "task"


def test_active_time_does_not_bridge_idle_gaps():
    minute = 60 * 1_000_000

    assert work_reporting._bounded_active_seconds({0, 5 * minute, 36 * minute}) == 5 * 60


def _priced_metrics(
    *,
    cost: float,
    priced_tokens: int = 1000,
    total_tokens: int = 1000,
    commits: int = 0,
    pull_requests: int = 0,
    additions: int = 0,
    deletions: int = 0,
) -> dict:
    """A metrics bucket with a priced cost side and chosen outcome denominators."""
    metrics = work_reporting._empty_metrics()
    metrics["total_tokens"] = total_tokens
    metrics["priced_tokens"] = priced_tokens
    metrics["estimated_cost_usd"] = cost
    metrics["git_commits"] = commits
    metrics["git_pull_requests"] = pull_requests
    metrics["additions"] = additions
    metrics["deletions"] = deletions
    return metrics


def test_cost_ratios_divide_cost_by_each_outcome_denominator():
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(cost=12.0, commits=8, pull_requests=4, additions=200, deletions=100)
    )

    assert finalized["estimated_cost_usd"] == 12.0
    assert finalized["cost_per_pr"] == 3.0            # 12.00 / 4 pull requests
    assert finalized["cost_per_commit"] == 1.5        # 12.00 / 8 commits
    assert finalized["changed_lines"] == 300
    assert finalized["cost_per_changed_line"] == 0.04  # 12.00 / 300 changed lines


def test_cost_ratio_is_suppressed_not_zero_when_denominator_is_zero():
    # A period with real cost but no pull requests must not claim the pull requests
    # were free, and must not raise. Suppression is None, never 0.
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(cost=9.0, commits=3, pull_requests=0, additions=0, deletions=0)
    )

    assert finalized["cost_per_pr"] is None
    assert finalized["cost_per_changed_line"] is None
    # the denominator that does exist still yields a ratio
    assert finalized["cost_per_commit"] == 3.0


def test_bucket_with_no_ai_usage_suppresses_ratios_instead_of_publishing_zero():
    # A repo or work item with git activity but NO attributed AI usage has
    # estimated_cost_usd 0.0 (has_cost is true when total_tokens == 0), which means
    # "never measured", not "free". Publishing 0.0 ratios would rank unmeasured work
    # as the cheapest in the report, and pricing_coverage_pct is None here so nothing
    # would qualify it.
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(
            cost=0.0, priced_tokens=0, total_tokens=0,
            commits=1, pull_requests=1, additions=400, deletions=20,
        )
    )

    assert finalized["estimated_cost_usd"] == 0.0
    assert finalized["pricing_coverage_pct"] is None
    assert finalized["cost_per_commit"] is None
    assert finalized["cost_per_pr"] is None
    assert finalized["cost_per_changed_line"] is None


def test_small_positive_ratio_does_not_round_down_to_a_misleading_zero():
    # A fully priced bucket whose per-line cost is below 1e-6 must not emit 0.0,
    # which would be byte-identical to the suppressed/never-measured case.
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(cost=2.0, commits=0, pull_requests=0, additions=8_000_000, deletions=0)
    )

    assert finalized["changed_lines"] == 8_000_000
    ratio = finalized["cost_per_changed_line"]
    assert ratio is not None
    assert ratio > 0.0
    assert ratio == pytest.approx(2.0 / 8_000_000)


def test_cost_ratios_are_suppressed_when_the_cost_side_is_unpriced():
    # priced_tokens == 0 with tokens present means the cost side is not priced, so
    # estimated_cost_usd is None; a ratio built on it would be fiction.
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(
            cost=0.0, priced_tokens=0, total_tokens=500,
            commits=5, pull_requests=2, additions=50, deletions=50,
        )
    )

    assert finalized["estimated_cost_usd"] is None
    assert finalized["cost_per_pr"] is None
    assert finalized["cost_per_commit"] is None
    assert finalized["cost_per_changed_line"] is None


def test_cost_ratios_travel_with_pricing_coverage():
    # Every ratio must be readable next to how much of the cost side was priced.
    finalized = work_reporting._finalize_metrics(
        _priced_metrics(
            cost=10.0, priced_tokens=250, total_tokens=1000,
            commits=2, pull_requests=1, additions=5, deletions=5,
        )
    )

    assert finalized["pricing_coverage_pct"] == 25.0
    assert finalized["cost_per_pr"] == 10.0
    # Substance of the rule: whenever a ratio is published, the coverage that
    # qualifies it must be published too, so a reader can never see a ratio without
    # knowing how much of its cost side was actually priced.
    published = [
        finalized[field]
        for field in ("cost_per_pr", "cost_per_commit", "cost_per_changed_line")
        if finalized[field] is not None
    ]
    assert published, "expected at least one ratio for this fixture"
    assert finalized["pricing_coverage_pct"] is not None
