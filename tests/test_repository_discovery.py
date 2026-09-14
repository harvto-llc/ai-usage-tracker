import json
import sqlite3
import subprocess

import pytest

from src import repository_discovery, usage_ledger, work_ledger


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


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
    (path / "README.md").write_text("test\n")
    git(path, "add", "README.md")
    git(path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    return path


def test_discovers_nested_cwd_without_scanning_or_enabling(repository):
    nested = repository / "src" / "nested"
    nested.mkdir(parents=True)

    result = repository_discovery.discover_repository(nested, now=100)

    assert result["path"] == str(repository)
    assert result["branch"] == "main"
    assert result["identity_kind"] == "root_commit"
    repos = work_ledger.list_repositories()
    assert len(repos) == 1
    assert repos[0]["enabled"] is False
    assert repos[0]["remote_fingerprint"] is None


def test_explicit_enablement_survives_later_session_discovery(repository):
    first = repository_discovery.discover_repository(repository, enabled=True, now=100)
    second = repository_discovery.discover_repository(repository, enabled=False, now=200)

    assert first["repository_id"] == second["repository_id"]
    repo = work_ledger.list_repositories()[0]
    assert repo["enabled"] is True
    assert repo["first_seen_at"] == 100
    assert repo["last_seen_at"] == 200


def test_linked_worktree_shares_repository_identity(repository, tmp_path):
    worktree = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "feature", str(worktree))

    main = repository_discovery.discover_repository(repository, now=100)
    linked = repository_discovery.discover_repository(worktree, now=100)

    assert main["repository_id"] == linked["repository_id"]
    assert main["worktree_id"] != linked["worktree_id"]
    with sqlite3.connect(work_ledger._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM repository_worktrees").fetchone()[0] == 2
    assert work_ledger.list_repositories()[0]["display_name"] == "repo"


def test_repository_relocation_keeps_root_commit_identity(repository, tmp_path):
    before = repository_discovery.discover_repository(repository, now=100)
    relocated = tmp_path / "relocated"
    repository.rename(relocated)

    after = repository_discovery.discover_repository(relocated, now=200)

    assert before["repository_id"] == after["repository_id"]
    assert before["worktree_id"] == after["worktree_id"]
    repo = work_ledger.list_repositories()[0]
    assert repo["common_dir"] == str(relocated / ".git")


def test_remote_fingerprint_ignores_credentials_and_transport():
    https = repository_discovery.normalize_remote_identity("https://token@github.com/Owner/Repo.git")
    ssh = repository_discovery.normalize_remote_identity("git@github.com:Owner/Repo.git")

    assert https == "github.com/Owner/Repo"
    assert ssh == https
    assert "token" not in https


def test_non_git_and_missing_candidates_are_ignored(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert repository_discovery.discover_repository(plain) is None
    assert repository_discovery.discover_repository(tmp_path / "missing") is None
    assert repository_discovery.discover_candidates([plain, tmp_path / "missing"]) == []


def test_session_repository_uses_loop_manifest_origin(repository, tmp_path):
    run_dir = tmp_path / ".loop" / "runs" / "repo-abc" / "36"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "repoId": "repo-abc",
        "runId": "36",
        "cwd": str(repository),
    }))

    result = repository_discovery.discover_session_repository(run_dir, now=100)

    assert result["path"] == str(repository)
    assert work_ledger.list_repositories()[0]["display_name"] == "repo"


def test_session_repository_matches_known_temporary_workspace(repository, tmp_path):
    known = repository_discovery.discover_repository(repository, enabled=True, now=100)
    wrapper = tmp_path / "repo-worker-copy"
    wrapper.mkdir()

    result = repository_discovery.discover_session_repository(wrapper, now=200)

    assert result["repository_id"] == known["repository_id"]
    assert work_ledger.list_repositories()[0]["enabled"] is True
