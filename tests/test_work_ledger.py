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


def _schema(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return [(row[1], row[2], row[3], row[4], row[5]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_init_is_idempotent_and_collection_defaults_off(activity_db):
    work_ledger.init()
    work_ledger.init()

    status = work_ledger.diagnostics()

    assert status["schema_version"] == 7
    assert status["settings"]["collection_enabled"] is False
    assert status["settings"]["retention_days"] == 365
    assert status["counts"] == {
        "repositories": 0,
        "repository_worktrees": 0,
        "ai_sessions": 0,
        "activity_events": 0,
        "work_items": 0,
        "work_attribution_intervals": 0,
        "session_labels": 0,
        "session_work_assignments": 0,
    }


def test_fresh_and_upgraded_databases_have_same_work_schema(activity_db):
    with sqlite3.connect(activity_db) as conn:
        conn.execute("CREATE TABLE usage_ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    work_ledger.init()
    upgraded = {
        table: _schema(activity_db, table)
        for table in (
            "repositories",
            "repository_worktrees",
            "ai_sessions",
            "activity_events",
            "git_index_state",
            "provider_activity_cursors",
            "work_items",
            "work_attribution_intervals",
            "session_labels",
            "session_work_assignments",
            "work_refresh_state",
        )
    }

    fresh_db = activity_db.with_name("fresh.db")
    os_value = str(fresh_db)
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv("USAGE_TRACKER_ACTIVITY_DB", os_value)
        usage_ledger._initialized_paths.discard(os_value)
        work_ledger._initialized_paths.discard(os_value)
        work_ledger.init()
    fresh = {
        table: _schema(fresh_db, table)
        for table in (
            "repositories",
            "repository_worktrees",
            "ai_sessions",
            "activity_events",
            "git_index_state",
            "provider_activity_cursors",
            "work_items",
            "work_attribution_intervals",
            "session_labels",
            "session_work_assignments",
            "work_refresh_state",
        )
    }
    assert upgraded == fresh


def test_collection_configuration_is_explicit_and_normalized(tmp_path):
    state = work_ledger.configure_collection(
        enabled=True,
        explicit_roots=[str(tmp_path / "b"), str(tmp_path / "a"), str(tmp_path / "b")],
        retention_days=90,
        now=100,
    )

    assert state["collection_enabled"] is True
    assert state["explicit_roots"] == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert state["retention_days"] == 90
    assert state["paused_at"] is None

    paused = work_ledger.configure_collection(enabled=False, now=200)
    assert paused["collection_enabled"] is False
    assert paused["paused_at"] == 200


def test_repository_and_worktree_upserts_preserve_manual_enablement():
    repository = {
        "id": "repo_1",
        "display_name": "usage-tracker",
        "identity_kind": "remote",
        "common_dir": "/tmp/repo/.git",
        "remote_fingerprint": "abc",
        "enabled": True,
    }
    work_ledger.upsert_repository(repository, now=100)
    repository["display_name"] = "renamed"
    repository["enabled"] = False
    work_ledger.upsert_repository(repository, now=200)
    work_ledger.upsert_worktree({
        "id": "worktree_1",
        "repository_id": "repo_1",
        "worktree_key": "main",
        "path": "/tmp/repo",
        "git_dir": "/tmp/repo/.git",
        "branch": "main",
        "head_sha": "abc123",
    }, now=200)

    with sqlite3.connect(work_ledger._db_path()) as conn:
        repo = conn.execute("SELECT display_name, enabled, first_seen_at, last_seen_at FROM repositories").fetchone()
        tree = conn.execute("SELECT repository_id, branch, head_sha FROM repository_worktrees").fetchone()
    assert repo == ("renamed", 1, 100, 200)
    assert tree == ("repo_1", "main", "abc123")


def test_activity_event_is_idempotent_and_rejects_content_bodies():
    event = {
        "event_key": "git:repo:commit:abc",
        "kind": "git_commit",
        "occurred_at_us": 1_000_000,
        "source": "git",
        "metadata": {"branch": "main", "files_changed": 2, "file_paths": ["src/a.py"]},
    }
    first = work_ledger.upsert_activity_event(event, now=10)
    event["metadata"]["files_changed"] = 3
    second = work_ledger.upsert_activity_event(event, now=20)

    assert first == second
    with sqlite3.connect(work_ledger._db_path()) as conn:
        rows = conn.execute("SELECT metadata_json, first_seen_at, last_seen_at FROM activity_events").fetchall()
    assert len(rows) == 1
    assert '"files_changed":3' in rows[0][0]
    assert rows[0][1:] == (10, 20)

    with pytest.raises(work_ledger.PrivacyBoundaryError, match="diff"):
        work_ledger.upsert_activity_event({**event, "event_key": "bad", "metadata": {"diff": "secret"}})
    with pytest.raises(work_ledger.PrivacyBoundaryError, match="prompt"):
        work_ledger.validate_event_metadata({"prompt": "secret"})


def test_ai_session_upsert_normalizes_provider_models_and_totals():
    session = {
        "provider": "Codex",
        "provider_session_id": "session-1",
        "source_path": "/tmp/session.jsonl",
        "started_at_us": 1_000_000,
        "ended_at_us": 2_000_000,
        "cwd": "/tmp/repo",
        "models": ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.5"],
        "messages": 10,
        "input_tokens": 100,
    }
    session_id = work_ledger.upsert_ai_session(session, now=10)
    session["messages"] = 12
    session["input_tokens"] = 150
    assert work_ledger.upsert_ai_session(session, now=20) == session_id

    with sqlite3.connect(work_ledger._db_path()) as conn:
        row = conn.execute(
            "SELECT provider, models_json, messages, input_tokens, first_seen_at, last_seen_at FROM ai_sessions"
        ).fetchone()
    assert row == ("codex", '["gpt-5.5","gpt-5.6-sol"]', 12, 150, 10, 20)

    with pytest.raises(ValueError, match="Claude and Codex"):
        work_ledger.upsert_ai_session({"provider": "cursor", "provider_session_id": "x"})


def test_session_nickname_and_work_assignment_lifecycle():
    project = work_ledger.create_work_item(kind="project", name="Usage Tracker", now=1)
    session_id = work_ledger.upsert_ai_session(
        {"provider": "codex", "provider_session_id": "session-1"},
        now=1,
    )

    assert work_ledger.set_session_nickname(session_id, "  Cost report  ", now=2)
    assert work_ledger.set_session_work_assignment(
        session_id,
        mode="work_item",
        work_item_id=project["id"],
        now=3,
    )
    session = work_ledger.list_ai_sessions()[0]
    assert session["nickname"] == "Cost report"
    assert session["assignment_mode"] == "work_item"
    assert session["assigned_work_name"] == "Usage Tracker"

    assert work_ledger.set_session_work_assignment(session_id, mode="unassigned", now=4)
    assert work_ledger.list_ai_sessions()[0]["assignment_mode"] == "unassigned"
    assert work_ledger.set_session_work_assignment(session_id, mode="automatic", now=5)
    assert work_ledger.list_ai_sessions()[0]["assignment_mode"] is None
    assert work_ledger.set_session_nickname(session_id, None, now=6)
    assert work_ledger.list_ai_sessions()[0]["nickname"] is None


def test_archiving_work_item_removes_session_override():
    project = work_ledger.create_work_item(kind="project", name="Usage Tracker", now=1)
    session_id = work_ledger.upsert_ai_session(
        {"provider": "claude", "provider_session_id": "session-1"},
        now=1,
    )
    work_ledger.set_session_work_assignment(
        session_id,
        mode="work_item",
        work_item_id=project["id"],
        now=2,
    )

    assert work_ledger.archive_work_item(project["id"], now=3)
    assert work_ledger.list_ai_sessions()[0]["assignment_mode"] is None


def test_stable_ids_are_repeatable_and_namespaced():
    assert work_ledger.stable_id("repo", "one") == work_ledger.stable_id("repo", "one")
    assert work_ledger.stable_id("repo", "one") != work_ledger.stable_id("worktree", "one")
