"""Live, privacy-bounded presentation state for local Claude and Codex sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from src import repository_discovery, work_ledger
from src.process_liveness import pid_alive


ACTIVE_SECONDS = 5 * 60
IDLE_SECONDS = 4 * 60 * 60
_DEFAULT_BRANCHES = frozenset({"main", "master", "trunk", "develop", "dev", "head"})


def _process_is_running(pid: object) -> bool:
    # Never os.kill(pid, 0) here: on Windows that terminates the user's Claude session.
    return pid_alive(pid)


def live_claude_sessions(home: Path | None = None) -> dict[str, dict]:
    sessions_dir = (home or Path.home()) / ".claude" / "sessions"
    live: dict[str, dict] = {}
    try:
        paths = sessions_dir.glob("*.json")
    except OSError:
        return live
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = payload.get("sessionId")
        if isinstance(session_id, str) and session_id and _process_is_running(payload.get("pid")):
            live[session_id] = payload
    return live


def sync_live_claude_sessions(
    *,
    home: Path | None = None,
    now: int | None = None,
) -> dict:
    if not work_ledger.settings()["collection_enabled"]:
        return {"provider": "claude", "status": "paused", "sessions": 0}
    metadata = live_claude_sessions(home)
    repositories_by_cwd: dict[str, dict | None] = {}
    for provider_session_id, payload in metadata.items():
        cwd = str(payload.get("cwd") or "").strip() or None
        if cwd and cwd not in repositories_by_cwd:
            repositories_by_cwd[cwd] = repository_discovery.discover_session_repository(
                cwd,
                enabled=False,
            )
        repository = repositories_by_cwd.get(cwd) if cwd else None
        started_ms = payload.get("startedAt")
        updated_ms = payload.get("updatedAt") or payload.get("statusUpdatedAt") or started_ms
        work_ledger.upsert_session_presence({
            "provider": "claude",
            "provider_session_id": provider_session_id,
            "started_at_us": int(started_ms) * 1_000 if started_ms else None,
            "ended_at_us": int(updated_ms) * 1_000 if updated_ms else None,
            "source_modified_at_us": int(updated_ms) * 1_000 if updated_ms else None,
            "cwd": cwd,
            "repository_id": repository.get("repository_id") if repository else None,
            "worktree_id": repository.get("worktree_id") if repository else None,
            "branch": repository.get("branch") if repository else None,
            "head_sha": repository.get("head_sha") if repository else None,
            "native_title": str(payload.get("name") or "").strip()[:120] or None,
        }, now=now)
    return {"provider": "claude", "status": "ok", "sessions": len(metadata)}


def _last_activity_at_us(session: dict) -> int | None:
    values = [
        session.get("source_modified_at_us"),
        session.get("ended_at_us"),
        session.get("started_at_us"),
    ]
    normalized = [int(value) for value in values if value is not None]
    return max(normalized) if normalized else None


def _runtime_state(
    session: dict,
    *,
    live_claude: dict[str, dict],
    now_us: int,
) -> str:
    if session.get("provider") == "claude":
        metadata = live_claude.get(str(session.get("provider_session_id")))
        if metadata is not None:
            status = str(metadata.get("status") or "").lower()
            if status == "busy":
                return "running"
            if status == "idle":
                return "idle"
            return "running"
    last_activity = _last_activity_at_us(session)
    if last_activity is None:
        return "ended"
    age_seconds = max(0, (now_us - last_activity) / 1_000_000)
    if age_seconds <= ACTIVE_SECONDS:
        return "active"
    if age_seconds <= IDLE_SECONDS:
        return "idle"
    return "ended"


def _fallback_name(session: dict) -> str:
    branch = str(session.get("branch") or "").strip()
    if branch and branch.lower() not in _DEFAULT_BRANCHES:
        return branch
    repository_name = str(session.get("repository_name") or "").strip()
    if repository_name:
        return repository_name
    cwd = str(session.get("cwd") or "").strip()
    if cwd:
        name = Path(cwd).name
        if name:
            return name
    provider = str(session.get("provider") or "Session").title()
    short_id = str(session.get("provider_session_id") or session.get("id") or "")[:8]
    return f"{provider} {short_id}".strip()


def _work_label(session: dict, default_work: dict) -> tuple[str, str]:
    mode = session.get("assignment_mode")
    if mode == "unassigned":
        return "Unassigned", "session"
    if mode == "work_item":
        name = str(session.get("assigned_work_name") or "Assigned work")
        parent = str(session.get("assigned_parent_name") or "")
        return (f"{parent} / {name}" if parent else name), "session"
    if default_work.get("state") == "active":
        name = str(default_work.get("name") or "Default work")
        parent = str(default_work.get("parent_name") or "")
        return (f"{parent} / {name}" if parent else name), "default"
    repository = str(session.get("repository_name") or "").strip()
    if repository:
        return repository, "automatic"
    return "Automatic", "automatic"


def import_agent_tts_nicknames(
    sessions: list[dict] | None = None,
    *,
    home: Path | None = None,
    now: int | None = None,
) -> int:
    root = home or Path.home()
    nickname_path = root / ".local" / "share" / "ai-agent-tts" / "session-nicknames.json"
    try:
        values = json.loads(nickname_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(values, dict):
        return 0
    imported = 0
    for session in sessions or work_ledger.list_ai_sessions(limit=2_000):
        if session.get("nickname") or not session.get("source_path"):
            continue
        path_hash = hashlib.md5(str(session["source_path"]).encode()).hexdigest()
        nickname = values.get(path_hash)
        if not isinstance(nickname, str) or not nickname.strip():
            continue
        if work_ledger.set_session_nickname(
            str(session["id"]),
            nickname,
            source="agent_tts_import",
            overwrite=False,
            now=now,
        ):
            imported += 1
    return imported


def list_session_views(
    *,
    provider: str | None = None,
    repository_id: str | None = None,
    active_only: bool = False,
    limit: int = 100,
    now: float | None = None,
    home: Path | None = None,
) -> list[dict]:
    rows = work_ledger.list_ai_sessions(
        provider=provider,
        repository_id=repository_id,
        limit=max(limit, 500 if active_only else limit),
    )
    if import_agent_tts_nicknames(rows, home=home):
        rows = work_ledger.list_ai_sessions(
            provider=provider,
            repository_id=repository_id,
            limit=max(limit, 500 if active_only else limit),
        )
    now_us = round((now if now is not None else time.time()) * 1_000_000)
    live_claude = live_claude_sessions(home)
    default_work = work_ledger.active_work_context()
    result: list[dict] = []
    for row in rows:
        runtime_state = _runtime_state(
            row,
            live_claude=live_claude,
            now_us=now_us,
        )
        if active_only and runtime_state == "ended":
            continue
        work_label, attribution_source = _work_label(row, default_work)
        nickname = str(row.get("nickname") or "").strip() or None
        native_title = str(row.get("native_title") or "").strip() or None
        result.append({
            **row,
            "display_name": native_title or nickname or _fallback_name(row),
            "runtime_state": runtime_state,
            "last_activity_at_us": _last_activity_at_us(row),
            "assignment_mode": row.get("assignment_mode") or "automatic",
            "effective_work_label": work_label,
            "attribution_source": attribution_source,
        })
        if len(result) >= max(1, min(int(limit), 500)):
            break
    return result
