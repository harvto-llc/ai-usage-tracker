import subprocess

import pytest

from src import git_activity, repository_discovery, usage_ledger, work_ledger


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit(path, message):
    git(path, "add", "-A")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "README.md").write_text("initial\n")
    commit(path, "initial commit")
    return path


def enabled_repository(repository):
    work_ledger.configure_collection(enabled=True, retention_days=3650, now=1)
    return repository_discovery.discover_repository(repository, enabled=True, now=1)


def test_collection_must_be_explicitly_enabled(repository):
    discovered = repository_discovery.discover_repository(repository, enabled=True, now=1)

    result = git_activity.index_repository(discovered, now=10)

    assert result["status"] == "paused"
    assert work_ledger.list_activity_events() == []


def test_initial_and_incremental_commit_indexing(repository):
    discovered = enabled_repository(repository)

    first = git_activity.index_repository(discovered, now=10)
    second = git_activity.index_repository(discovered, now=20)
    (repository / "src.py").write_text("print('safe')\n")
    new_sha = commit(repository, "add source")
    refreshed = repository_discovery.discover_repository(repository, now=30)
    third = git_activity.index_repository(refreshed, now=30)

    commits = work_ledger.list_activity_events(kind="git_commit")
    assert first["commits_scanned"] == 1
    assert second["commits_scanned"] == 0
    assert third["commits_scanned"] == 1
    assert len(commits) == 2
    latest = next(event for event in commits if event["source_cursor"] == new_sha)
    assert latest["metadata"]["file_paths"] == ["src.py"]
    assert latest["metadata"]["commit_subject"] == "add source"
    assert "diff" not in latest["metadata"]


def test_merged_pull_requests_are_inferred_from_local_commit_subjects(repository):
    discovered = enabled_repository(repository)
    git_activity.index_repository(discovered, now=10)
    (repository / "feature.py").write_text("value = 1\n")
    merge_sha = commit(repository, "ship local reporting (#42)")

    refreshed = repository_discovery.discover_repository(repository, now=20)
    result = git_activity.index_repository(refreshed, now=20)
    git_activity.index_repository(refreshed, now=30)

    pull_requests = work_ledger.list_activity_events(kind="git_pull_request")
    assert result["pull_requests_scanned"] == 1
    assert len(pull_requests) == 1
    assert pull_requests[0]["source_cursor"] == merge_sha
    assert pull_requests[0]["metadata"]["pull_request_number"] == 42
    assert "feature.py" not in str(pull_requests[0]["metadata"])


def test_existing_git_index_gets_one_time_pull_request_backfill(repository):
    (repository / "feature.py").write_text("value = 1\n")
    commit(repository, "ship local reporting (#42)")
    discovered = enabled_repository(repository)
    git_activity.index_repository(discovered, now=10)
    with work_ledger._connect() as conn:
        conn.execute("DELETE FROM activity_events WHERE kind = 'git_pull_request'")
        conn.execute(
            """UPDATE git_index_state
               SET pull_request_index_version = 0
               WHERE worktree_id = ?""",
            (discovered["worktree_id"],),
        )
        conn.commit()

    result = git_activity.index_repository(discovered, now=20)

    assert result["pull_requests_scanned"] == 1
    assert len(work_ledger.list_activity_events(kind="git_pull_request")) == 1
    assert work_ledger.git_index_state(discovered["worktree_id"])[
        "pull_request_index_version"
    ] == 1


def test_dirty_snapshot_contains_paths_and_counts_not_bodies(repository):
    discovered = enabled_repository(repository)
    git_activity.index_repository(discovered, now=10)
    (repository / "README.md").write_text("changed secret body\n")
    (repository / "new.txt").write_text("untracked body\n")

    result = git_activity.index_repository(discovered, now=20)
    dirty = work_ledger.list_activity_events(kind="git_dirty")[0]

    assert result["dirty"] is True
    assert dirty["metadata"]["file_paths"] == ["README.md", "new.txt"]
    serialized = str(dirty["metadata"])
    assert "secret body" not in serialized
    assert "untracked body" not in serialized

    (repository / "README.md").write_text("another body\n")
    git_activity.index_repository(discovered, now=25)
    snapshots = work_ledger.list_activity_events(kind="git_dirty", include_superseded=True)
    assert len(snapshots) == 2
    assert sum(event["is_current"] for event in snapshots) == 1

    git(repository, "reset", "--hard", "HEAD")
    (repository / "new.txt").unlink()
    git_activity.index_repository(discovered, now=30)
    assert work_ledger.list_activity_events(kind="git_dirty") == []


def test_amended_history_marks_superseded_commit(repository):
    discovered = enabled_repository(repository)
    git_activity.index_repository(discovered, now=10)
    old_sha = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("amended\n")
    git(repository, "add", "README.md")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--amend", "-m", "amended")
    new_sha = git(repository, "rev-parse", "HEAD")

    refreshed = repository_discovery.discover_repository(repository, now=20)
    result = git_activity.index_repository(refreshed, now=20)
    events = work_ledger.list_activity_events(kind="git_commit", include_superseded=True)

    assert result["history_reconciled"] is True
    assert {event["source_cursor"] for event in events} == {old_sha, new_sha}
    states = {event["source_cursor"]: event["is_current"] for event in events}
    assert states == {old_sha: False, new_sha: True}


def test_configured_roots_are_indexed_without_home_scan(repository, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    work_ledger.configure_collection(
        enabled=True,
        explicit_roots=[str(repository)],
        now=1,
    )

    result = git_activity.index_configured_repositories(
        session_cwds=[plain],
        now=10,
    )

    assert result["status"] == "ok"
    assert result["repositories"] == 1
    assert result["results"][0]["commits_scanned"] == 1
    assert work_ledger.list_repositories()[0]["enabled"] is True
