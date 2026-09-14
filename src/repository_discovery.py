"""Candidate-driven Git repository and worktree discovery."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

from src import work_ledger


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def _display_name(
    top_level: Path,
    common_dir: Path,
    remote_identity: str | None,
) -> str:
    if remote_identity:
        remote_name = remote_identity.rstrip("/").rsplit("/", 1)[-1]
        if remote_name:
            return remote_name
    if common_dir.name == ".git" and common_dir.parent.name:
        return common_dir.parent.name
    return top_level.name


def normalize_remote_identity(remote: str | None) -> str | None:
    if not remote:
        return None
    value = remote.strip()
    scp_match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", value)
    if scp_match and "://" not in value:
        host, path = scp_match.groups()
        normalized = f"{host.lower()}/{path}"
    else:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme and parsed.hostname:
            path = urllib.parse.unquote(parsed.path)
            normalized = f"{parsed.hostname.lower()}/{path.lstrip('/')}"
        else:
            normalized = str(Path(value).expanduser().resolve(strict=False))
    return normalized.removesuffix(".git").rstrip("/")


def discover_repository(candidate: str | Path, *, enabled: bool = False, now: int | None = None) -> dict | None:
    path = Path(candidate).expanduser().resolve(strict=False)
    if not path.exists():
        return None
    top_level_raw = _git(path, "rev-parse", "--show-toplevel")
    common_dir_raw = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    git_dir_raw = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    if not top_level_raw or not common_dir_raw or not git_dir_raw:
        return None

    top_level = Path(top_level_raw).resolve(strict=False)
    common_dir = Path(common_dir_raw).resolve(strict=False)
    git_dir = Path(git_dir_raw).resolve(strict=False)
    remote_identity = normalize_remote_identity(_git(path, "remote", "get-url", "origin"))
    remote_fingerprint = _fingerprint(remote_identity)
    roots = _git(path, "rev-list", "--max-parents=0", "--all")
    root_fingerprint = _fingerprint("\n".join(sorted(roots.splitlines()))) if roots else None

    repository_id = work_ledger.find_repository_id(
        common_dir=str(common_dir),
        remote_fingerprint=remote_fingerprint,
        root_commit_fingerprint=root_fingerprint,
    )
    if repository_id is None:
        if remote_fingerprint:
            identity_kind, identity_value = "remote", remote_fingerprint
        elif root_fingerprint:
            identity_kind, identity_value = "root_commit", root_fingerprint
        else:
            identity_kind, identity_value = "common_dir", str(common_dir)
        repository_id = work_ledger.stable_id("repo", identity_kind, identity_value)
    else:
        existing = next(
            (repo for repo in work_ledger.list_repositories(include_archived=True) if repo["id"] == repository_id),
            None,
        )
        identity_kind = existing["identity_kind"] if existing else "common_dir"

    work_ledger.upsert_repository(
        {
            "id": repository_id,
            "display_name": _display_name(top_level, common_dir, remote_identity),
            "identity_kind": identity_kind,
            "common_dir": str(common_dir),
            "remote_fingerprint": remote_fingerprint,
            "root_commit_fingerprint": root_fingerprint,
            "enabled": enabled,
        },
        now=now,
    )

    worktree_key = "main" if git_dir == common_dir else git_dir.name
    worktree_id = work_ledger.stable_id("worktree", repository_id, worktree_key)
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    head_sha = _git(path, "rev-parse", "--verify", "HEAD")
    work_ledger.upsert_worktree(
        {
            "id": worktree_id,
            "repository_id": repository_id,
            "worktree_key": worktree_key,
            "path": str(top_level),
            "git_dir": str(git_dir),
            "branch": branch,
            "head_sha": head_sha,
        },
        now=now,
    )
    return {
        "repository_id": repository_id,
        "worktree_id": worktree_id,
        "path": str(top_level),
        "common_dir": str(common_dir),
        "git_dir": str(git_dir),
        "branch": branch,
        "head_sha": head_sha,
        "identity_kind": identity_kind,
    }


def _loop_manifest_cwd(path: Path) -> Path | None:
    for depth, parent in enumerate((path, *path.parents)):
        if depth > 8:
            break
        manifest = parent / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not payload.get("repoId") or not payload.get("runId"):
            continue
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return Path(cwd).expanduser().resolve(strict=False)
    return None


def _known_repository_cwd(path: Path) -> Path | None:
    matches: dict[str, Path] = {}
    path_text = str(path).casefold()
    components = [part.casefold() for part in path.parts]
    for repository in work_ledger.list_repositories():
        common_dir = Path(str(repository["common_dir"])).expanduser().resolve(strict=False)
        root = common_dir.parent if common_dir.name == ".git" else None
        if root is None or not root.exists():
            continue
        display_name = str(repository["display_name"]).strip().casefold()
        encoded_root = str(root).replace("/", "-").casefold()
        named_wrapper = len(display_name) >= 4 and any(
            part == display_name
            or part.startswith(f"{display_name}-")
            or part.startswith(f"{display_name}_")
            for part in components
        )
        if encoded_root in path_text or named_wrapper:
            matches[str(repository["id"])] = root
    return next(iter(matches.values())) if len(matches) == 1 else None


def discover_session_repository(
    candidate: str | Path,
    *,
    enabled: bool = False,
    now: int | None = None,
) -> dict | None:
    """Resolve Git checkouts plus known loop and temporary workspace wrappers."""
    direct = discover_repository(candidate, enabled=enabled, now=now)
    if direct:
        return direct
    path = Path(candidate).expanduser().resolve(strict=False)
    provenance_cwd = _loop_manifest_cwd(path) or _known_repository_cwd(path)
    if provenance_cwd is None or provenance_cwd == path:
        return None
    return discover_repository(provenance_cwd, enabled=enabled, now=now)


def discover_candidates(candidates: list[str | Path], *, enabled: bool = False, now: int | None = None) -> list[dict]:
    discovered: dict[str, dict] = {}
    for candidate in candidates:
        result = discover_repository(candidate, enabled=enabled, now=now)
        if result:
            discovered[result["worktree_id"]] = result
    return list(discovered.values())
