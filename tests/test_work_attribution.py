import sqlite3

import pytest

from src import usage_ledger, work_ledger


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    return db_path


def test_project_task_story_hierarchy_is_validated_and_normalized():
    project = work_ledger.create_work_item(
        kind=" Project ",
        name="  Client   Portal ",
        color_hex="#12abef",
        now=10,
    )
    task = work_ledger.create_work_item(
        kind="task",
        name="Billing flow",
        parent_id=project["id"],
        now=20,
    )
    story = work_ledger.create_work_item(
        kind="story",
        name="Retry failed cards",
        parent_id=project["id"],
        now=30,
    )

    assert project["name"] == "Client Portal"
    assert project["color_hex"] == "#12ABEF"
    assert task["parent_id"] == project["id"]
    assert story["parent_id"] == project["id"]
    assert [item["kind"] for item in work_ledger.list_work_items()] == [
        "project",
        "story",
        "task",
    ]

    with pytest.raises(ValueError, match="cannot have a parent"):
        work_ledger.create_work_item(
            kind="project",
            name="Nested",
            parent_id=project["id"],
        )
    with pytest.raises(ValueError, match="require a project parent"):
        work_ledger.create_work_item(kind="task", name="Orphan")
    with pytest.raises(ValueError, match="active project"):
        work_ledger.create_work_item(
            kind="task",
            name="Nested task",
            parent_id=task["id"],
        )
    with pytest.raises(ValueError, match="#RRGGBB"):
        work_ledger.create_work_item(kind="project", name="Bad color", color_hex="red")


def test_switching_active_work_is_atomic_non_overlapping_and_idempotent(activity_db):
    repository_id = work_ledger.upsert_repository(
        {
            "id": "repo-1",
            "common_dir": "/repo/.git",
            "display_name": "repo",
            "identity_kind": "common_dir",
        },
        now=1,
    )
    project = work_ledger.create_work_item(
        kind="project",
        name="Project",
        repository_id=repository_id,
        color_hex="#12ABEF",
        now=1,
    )
    task = work_ledger.create_work_item(
        kind="task",
        name="Task",
        parent_id=project["id"],
        now=1,
    )

    active = work_ledger.switch_active_work_item(project["id"], at_us=100, now=1)
    unchanged = work_ledger.switch_active_work_item(project["id"], at_us=150, now=2)
    switched = work_ledger.switch_active_work_item(task["id"], at_us=200, now=3)
    stopped = work_ledger.switch_active_work_item(None, at_us=300, now=4)

    assert active["work_item_id"] == project["id"]
    assert unchanged["interval_id"] == active["interval_id"]
    assert switched["work_item_id"] == task["id"]
    assert switched["repository_id"] == repository_id
    assert switched["repository_name"] == "repo"
    assert switched["color_hex"] == "#12ABEF"
    assert stopped == {"state": "unassigned"}
    intervals = list(reversed(work_ledger.list_attribution_intervals()))
    assert [(row["work_item_id"], row["started_at_us"], row["ended_at_us"]) for row in intervals] == [
        (project["id"], 100, 200),
        (task["id"], 200, 300),
    ]
    with sqlite3.connect(activity_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM work_attribution_intervals WHERE ended_at_us IS NULL"
        ).fetchone()[0] == 0


def test_invalid_switch_does_not_close_current_interval():
    project = work_ledger.create_work_item(kind="project", name="Project", now=1)
    current = work_ledger.switch_active_work_item(project["id"], at_us=100, now=1)

    with pytest.raises(ValueError, match="active work item"):
        work_ledger.switch_active_work_item("missing", at_us=200, now=2)

    assert work_ledger.active_work_context()["interval_id"] == current["interval_id"]
    assert work_ledger.list_attribution_intervals()[0]["ended_at_us"] is None


def test_archiving_project_archives_children_and_stops_active_work():
    project = work_ledger.create_work_item(kind="project", name="Project", now=1)
    task = work_ledger.create_work_item(
        kind="task",
        name="Task",
        parent_id=project["id"],
        now=1,
    )
    work_ledger.switch_active_work_item(task["id"], at_us=100, now=1)

    assert work_ledger.archive_work_item(project["id"], at_us=250, now=2) is True

    assert work_ledger.active_work_context() == {"state": "unassigned"}
    assert work_ledger.list_work_items() == []
    archived = work_ledger.list_work_items(include_archived=True)
    assert {item["archived_at"] for item in archived} == {2}
    assert work_ledger.list_attribution_intervals()[0]["ended_at_us"] == 250
    assert work_ledger.archive_work_item("missing", now=3) is False
