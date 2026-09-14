"""Incremental, metadata-only Git activity indexing."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from src import repository_discovery, work_ledger


_PULL_REQUEST_PATTERNS = (
    re.compile(
        r"^Merge pull request #(?P<number>\d+)(?: from (?P<branch>\S+))?",
        re.IGNORECASE,
    ),
    re.compile(r"\s+\(#(?P<number>\d+)\)$"),
    re.compile(r"\s+\(!(?P<number>\d+)\)$"),
)


def _pull_request(subject: str) -> dict | None:
    for pattern in _PULL_REQUEST_PATTERNS:
        match = pattern.search(subject)
        if match:
            return {
                "number": int(match.group("number")),
                "branch": match.groupdict().get("branch"),
            }
    return None


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    return _run(path, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _since(cutoff: int) -> str:
    return "--since=" + datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()


def _commit_records(path: Path, revision: str, cutoff: int) -> list[dict]:
    result = _run(
        path,
        "log",
        "--reverse",
        "--date-order",
        "--no-renames",
        _since(cutoff),
        "--format=%x1e%H%x1f%P%x1f%at%x1f%ct%x1f%s",
        "--numstat",
        revision,
    )
    if result.returncode != 0:
        return []
    records = []
    for raw_record in result.stdout.split("\x1e"):
        block = raw_record.strip("\n")
        if not block:
            continue
        lines = block.splitlines()
        fields = lines[0].split("\x1f", 4)
        if len(fields) != 5:
            continue
        sha, parents, author_at, committer_at, subject = fields
        additions = deletions = 0
        file_paths = []
        for line in lines[1:]:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, file_path = parts
            file_paths.append(file_path)
            additions += int(added) if added.isdigit() else 0
            deletions += int(deleted) if deleted.isdigit() else 0
        records.append({
            "sha": sha,
            "parents": parents.split() if parents else [],
            "author_at_us": int(author_at) * 1_000_000,
            "committer_at_us": int(committer_at) * 1_000_000,
            "subject": " ".join(subject.split())[:200],
            "file_paths": sorted(file_paths),
            "additions": additions,
            "deletions": deletions,
            "pull_request": _pull_request(subject),
        })
    return records


def _reachable_shas(path: Path, cutoff: int) -> set[str]:
    result = _run(path, "rev-list", _since(cutoff), "HEAD")
    return set(result.stdout.splitlines()) if result.returncode == 0 else set()


def _numstat(path: Path, *args: str) -> tuple[int, int]:
    result = _run(path, "diff", "--numstat", *args)
    if result.returncode != 0:
        return 0, 0
    additions = deletions = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        additions += int(parts[0]) if parts[0].isdigit() else 0
        deletions += int(parts[1]) if parts[1].isdigit() else 0
    return additions, deletions


def _dirty_snapshot(path: Path) -> dict | None:
    result = _run(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if result.returncode != 0 or not result.stdout:
        return None
    entries = result.stdout.split("\0")
    status_counts: dict[str, int] = {}
    file_paths = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        path_value = entry[3:]
        status_counts[status] = status_counts.get(status, 0) + 1
        file_paths.append(path_value)
        if (status[0] in {"R", "C"} or status[1] in {"R", "C"}) and index < len(entries):
            old_path = entries[index]
            index += 1
            if old_path:
                file_paths.append(old_path)
    unstaged_add, unstaged_delete = _numstat(path, "HEAD", "--")
    staged_add, staged_delete = _numstat(path, "--cached", "HEAD", "--")
    return {
        "file_paths": sorted(set(file_paths)),
        "status_counts": status_counts,
        "additions": unstaged_add + staged_add,
        "deletions": unstaged_delete + staged_delete,
    }


def _dirty_hash(path: Path, dirty: dict) -> str:
    file_state = []
    for relative_path in dirty["file_paths"]:
        try:
            stat = (path / relative_path).stat()
            file_state.append((relative_path, stat.st_size, stat.st_mtime_ns))
        except OSError:
            file_state.append((relative_path, None, None))
    payload = {"metadata": dirty, "file_state": file_state}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def index_repository(discovery: dict, *, now: int | None = None) -> dict:
    timestamp = int(now if now is not None else time.time())
    ledger_settings = work_ledger.settings()
    repository_id = discovery["repository_id"]
    repository = next(
        (row for row in work_ledger.list_repositories() if row["id"] == repository_id),
        None,
    )
    if not ledger_settings["collection_enabled"] or not repository or not repository["enabled"]:
        return {"status": "paused", "commits_scanned": 0, "history_reconciled": False}

    path = Path(discovery["path"])
    worktree_id = discovery["worktree_id"]
    head_sha = discovery.get("head_sha")
    branch = discovery.get("branch")
    previous = work_ledger.git_index_state(worktree_id)
    retention_days = ledger_settings["retention_days"]
    cutoff = max(0, timestamp - retention_days * 86400)
    rewritten = False

    if not head_sha:
        work_ledger.update_git_index_state(
            worktree_id=worktree_id,
            repository_id=repository_id,
            head_sha=None,
            branch=branch,
            dirty_hash=None,
            error="repository has no HEAD",
            now=timestamp,
        )
        return {"status": "empty", "commits_scanned": 0, "history_reconciled": False}

    last_head = previous.get("last_head_sha") if previous else None
    needs_pull_request_backfill = not previous or int(
        previous.get("pull_request_index_version") or 0
    ) < 1
    if needs_pull_request_backfill:
        rewritten = bool(last_head and not _is_ancestor(path, last_head, head_sha))
        commits = _commit_records(path, "HEAD", cutoff)
    elif last_head == head_sha:
        commits = []
    elif last_head and _is_ancestor(path, last_head, head_sha):
        commits = _commit_records(path, f"{last_head}..{head_sha}", cutoff)
    else:
        rewritten = bool(last_head)
        commits = _commit_records(path, "HEAD", cutoff)

    for commit in commits:
        work_ledger.upsert_activity_event(
            {
                "event_key": f"git:{repository_id}:commit:{commit['sha']}",
                "kind": "git_commit",
                "occurred_at_us": commit["committer_at_us"],
                "repository_id": repository_id,
                "worktree_id": worktree_id,
                "source": "git",
                "source_cursor": commit["sha"],
                "metadata": {
                    "commit_sha": commit["sha"],
                    "commit_subject": commit["subject"],
                    "parent_shas": commit["parents"],
                    "author_at_us": commit["author_at_us"],
                    "committer_at_us": commit["committer_at_us"],
                    "merge_commit": len(commit["parents"]) > 1,
                    "file_paths": commit["file_paths"],
                    "files_changed": len(commit["file_paths"]),
                    "additions": commit["additions"],
                    "deletions": commit["deletions"],
                    "branch": branch,
                    "head_sha": head_sha,
                },
            },
            now=timestamp,
        )
        pull_request = commit.get("pull_request")
        if pull_request:
            work_ledger.upsert_activity_event(
                {
                    "event_key": (
                        f"git:{repository_id}:pull_request:"
                        f"{pull_request['number']}"
                    ),
                    "kind": "git_pull_request",
                    "occurred_at_us": commit["committer_at_us"],
                    "repository_id": repository_id,
                    "worktree_id": worktree_id,
                    "source": "git",
                    "source_cursor": commit["sha"],
                    "metadata": {
                        "commit_sha": commit["sha"],
                        "pull_request_number": pull_request["number"],
                        "pull_request_branch": pull_request.get("branch"),
                        "merge_commit": len(commit["parents"]) > 1,
                    },
                },
                now=timestamp,
            )

    superseded = 0
    if rewritten:
        superseded = work_ledger.reconcile_current_git_commits(
            repository_id,
            _reachable_shas(path, cutoff),
            cutoff_us=cutoff * 1_000_000,
            now=timestamp,
        )

    if not previous or previous.get("last_head_sha") != head_sha or previous.get("last_branch") != branch:
        work_ledger.upsert_activity_event(
            {
                "event_key": f"git:{worktree_id}:head:{timestamp}:{branch or 'detached'}:{head_sha}",
                "kind": "git_head",
                "occurred_at_us": timestamp * 1_000_000,
                "repository_id": repository_id,
                "worktree_id": worktree_id,
                "source": "git",
                "source_cursor": head_sha,
                "metadata": {"branch": branch, "head_sha": head_sha},
            },
            now=timestamp,
        )

    dirty = _dirty_snapshot(path)
    dirty_hash = None
    if dirty:
        dirty_hash = _dirty_hash(path, dirty)
        if not previous or previous.get("dirty_hash") != dirty_hash:
            if previous and previous.get("dirty_hash"):
                work_ledger.set_activity_event_current(
                    f"git:{worktree_id}:dirty:{previous['dirty_hash']}",
                    False,
                    now=timestamp,
                )
            work_ledger.upsert_activity_event(
                {
                    "event_key": f"git:{worktree_id}:dirty:{dirty_hash}",
                    "kind": "git_dirty",
                    "occurred_at_us": timestamp * 1_000_000,
                    "repository_id": repository_id,
                    "worktree_id": worktree_id,
                    "source": "git",
                    "source_cursor": dirty_hash,
                    "metadata": {**dirty, "branch": branch, "head_sha": head_sha},
                },
                now=timestamp,
            )
    elif previous and previous.get("dirty_hash"):
        work_ledger.set_activity_event_current(
            f"git:{worktree_id}:dirty:{previous['dirty_hash']}",
            False,
            now=timestamp,
        )

    work_ledger.update_git_index_state(
        worktree_id=worktree_id,
        repository_id=repository_id,
        head_sha=head_sha,
        branch=branch,
        dirty_hash=dirty_hash,
        now=timestamp,
    )
    return {
        "status": "ok",
        "commits_scanned": len(commits),
        "pull_requests_scanned": sum(
            commit.get("pull_request") is not None for commit in commits
        ),
        "history_reconciled": rewritten,
        "commits_superseded": superseded,
        "dirty": bool(dirty),
    }


def index_configured_repositories(
    *,
    session_cwds: list[str | Path] | None = None,
    now: int | None = None,
) -> dict:
    ledger_settings = work_ledger.settings()
    if not ledger_settings["collection_enabled"]:
        return {"status": "paused", "repositories": 0, "results": []}

    discovered: dict[str, dict] = {}
    explicit = repository_discovery.discover_candidates(
        ledger_settings["explicit_roots"],
        enabled=True,
        now=now,
    )
    for item in explicit:
        work_ledger.set_repository_enabled(item["repository_id"], True, now=now)
        discovered[item["worktree_id"]] = item
    for item in repository_discovery.discover_candidates(session_cwds or [], enabled=False, now=now):
        discovered[item["worktree_id"]] = item

    results = [index_repository(item, now=now) for item in discovered.values()]
    return {"status": "ok", "repositories": len(discovered), "results": results}
