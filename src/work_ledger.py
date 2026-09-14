"""Privacy-bounded persistence for repository and AI work activity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from src import usage_ledger


SCHEMA_VERSION = 7
WORK_ITEM_KINDS = frozenset({"project", "task", "story"})
SESSION_ASSIGNMENT_MODES = frozenset({"unassigned", "work_item"})
ALLOWED_EVENT_METADATA = frozenset({
    "additions",
    "author_at_us",
    "branch",
    "command_category",
    "commit_sha",
    "commit_subject",
    "committer_at_us",
    "deletions",
    "exit_code",
    "file_paths",
    "files_changed",
    "head_sha",
    "merge_commit",
    "model",
    "parent_shas",
    "pull_request_branch",
    "pull_request_number",
    "request_count",
    "result",
    "status_counts",
    "tool_name",
})

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


class PrivacyBoundaryError(ValueError):
    pass


def _db_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_ACTIVITY_DB")
    return Path(configured).expanduser() if configured else Path.home() / ".usage-tracker" / "activity.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    usage_ledger.init()
    path_key = str(_db_path())
    if path_key in _initialized_paths:
        return
    with _init_lock:
        if path_key in _initialized_paths:
            return
        with closing(_connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_ledger_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_ledger_settings(
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    collection_enabled INTEGER NOT NULL DEFAULT 0,
                    paused_at INTEGER,
                    explicit_roots_json TEXT NOT NULL DEFAULT '[]',
                    retention_days INTEGER NOT NULL DEFAULT 365,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repositories(
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    identity_kind TEXT NOT NULL,
                    common_dir TEXT NOT NULL,
                    remote_fingerprint TEXT,
                    root_commit_fingerprint TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_repositories_common_dir
                ON repositories(common_dir);

                CREATE INDEX IF NOT EXISTS idx_repositories_remote
                ON repositories(remote_fingerprint);

                CREATE TABLE IF NOT EXISTS repository_worktrees(
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES repositories(id),
                    worktree_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    git_dir TEXT NOT NULL,
                    branch TEXT,
                    head_sha TEXT,
                    is_present INTEGER NOT NULL DEFAULT 1,
                    archived_at INTEGER,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE(repository_id, worktree_key)
                );

                CREATE INDEX IF NOT EXISTS idx_repository_worktrees_path
                ON repository_worktrees(path);

                CREATE TABLE IF NOT EXISTS ai_sessions(
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_session_id TEXT NOT NULL,
                    source_file_key TEXT,
                    source_path TEXT,
                    started_at_us INTEGER,
                    ended_at_us INTEGER,
                    cwd TEXT,
                    repository_id TEXT REFERENCES repositories(id),
                    worktree_id TEXT REFERENCES repository_worktrees(id),
                    branch TEXT,
                    head_sha TEXT,
                    native_title TEXT,
                    source_modified_at_us INTEGER,
                    models_json TEXT NOT NULL DEFAULT '[]',
                    messages INTEGER NOT NULL DEFAULT 0,
                    user_messages INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL,
                    estimated_credits REAL,
                    source_health TEXT NOT NULL DEFAULT 'current',
                    archived_at INTEGER,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE(provider, provider_session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_sessions_time
                ON ai_sessions(started_at_us, ended_at_us);

                CREATE INDEX IF NOT EXISTS idx_ai_sessions_repository
                ON ai_sessions(repository_id, started_at_us);

                CREATE TABLE IF NOT EXISTS activity_events(
                    id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    occurred_at_us INTEGER NOT NULL,
                    repository_id TEXT REFERENCES repositories(id),
                    worktree_id TEXT REFERENCES repository_worktrees(id),
                    ai_session_id TEXT REFERENCES ai_sessions(id),
                    provider TEXT,
                    source TEXT NOT NULL,
                    source_cursor TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    is_current INTEGER NOT NULL DEFAULT 1,
                    archived_at INTEGER,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_activity_events_time
                ON activity_events(occurred_at_us);

                CREATE INDEX IF NOT EXISTS idx_activity_events_repository
                ON activity_events(repository_id, occurred_at_us);

                CREATE INDEX IF NOT EXISTS idx_activity_events_session
                ON activity_events(ai_session_id, occurred_at_us);

                CREATE TABLE IF NOT EXISTS git_index_state(
                    worktree_id TEXT PRIMARY KEY REFERENCES repository_worktrees(id),
                    repository_id TEXT NOT NULL REFERENCES repositories(id),
                    last_head_sha TEXT,
                    last_branch TEXT,
                    dirty_hash TEXT,
                    last_scan_at INTEGER,
                    last_error TEXT,
                    pull_request_index_version INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_activity_cursors(
                    provider TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_key TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    offset_bytes INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    session_id TEXT,
                    cwd TEXT,
                    cursor_hash TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(provider, path)
                );

                CREATE INDEX IF NOT EXISTS idx_provider_activity_cursors_file
                ON provider_activity_cursors(provider, file_key);

                CREATE TABLE IF NOT EXISTS provider_title_cursors(
                    provider TEXT NOT NULL,
                    path TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    offset_bytes INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    cursor_hash TEXT NOT NULL DEFAULT '',
                    native_title TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(provider, path)
                );

                CREATE TABLE IF NOT EXISTS work_items(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('project', 'task', 'story')),
                    parent_id TEXT REFERENCES work_items(id),
                    repository_id TEXT REFERENCES repositories(id),
                    name TEXT NOT NULL,
                    color_hex TEXT,
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_active_name
                ON work_items(kind, COALESCE(parent_id, ''), name COLLATE NOCASE)
                WHERE archived_at IS NULL;

                CREATE INDEX IF NOT EXISTS idx_work_items_parent
                ON work_items(parent_id, archived_at);

                CREATE TABLE IF NOT EXISTS session_labels(
                    ai_session_id TEXT PRIMARY KEY REFERENCES ai_sessions(id) ON DELETE CASCADE,
                    nickname TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'usage_tracker',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_work_assignments(
                    ai_session_id TEXT PRIMARY KEY REFERENCES ai_sessions(id) ON DELETE CASCADE,
                    assignment_mode TEXT NOT NULL CHECK(assignment_mode IN ('unassigned', 'work_item')),
                    work_item_id TEXT REFERENCES work_items(id),
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CHECK(
                        (assignment_mode = 'unassigned' AND work_item_id IS NULL)
                        OR (assignment_mode = 'work_item' AND work_item_id IS NOT NULL)
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_session_work_assignments_item
                ON session_work_assignments(work_item_id);

                CREATE TABLE IF NOT EXISTS work_attribution_intervals(
                    id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL REFERENCES work_items(id),
                    started_at_us INTEGER NOT NULL,
                    ended_at_us INTEGER,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at INTEGER NOT NULL,
                    CHECK(ended_at_us IS NULL OR ended_at_us >= started_at_us)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_attribution_one_active
                ON work_attribution_intervals((1)) WHERE ended_at_us IS NULL;

                CREATE INDEX IF NOT EXISTS idx_work_attribution_time
                ON work_attribution_intervals(started_at_us, ended_at_us);

                CREATE TABLE IF NOT EXISTS work_refresh_state(
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    status TEXT NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    last_error TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                );
                """
            )
            now = int(time.time())
            git_state_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(git_index_state)").fetchall()
            }
            if "pull_request_index_version" not in git_state_columns:
                conn.execute(
                    """ALTER TABLE git_index_state
                       ADD COLUMN pull_request_index_version INTEGER NOT NULL DEFAULT 0"""
                )
            session_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(ai_sessions)").fetchall()
            }
            if "native_title" not in session_columns:
                conn.execute("ALTER TABLE ai_sessions ADD COLUMN native_title TEXT")
            if "source_modified_at_us" not in session_columns:
                conn.execute(
                    "ALTER TABLE ai_sessions ADD COLUMN source_modified_at_us INTEGER"
                )
            conn.execute(
                """INSERT OR IGNORE INTO work_ledger_settings(
                       id, collection_enabled, explicit_roots_json, retention_days, updated_at
                   ) VALUES (1, 0, '[]', 365, ?)""",
                (now,),
            )
            conn.execute(
                """INSERT INTO work_ledger_meta(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                """INSERT OR IGNORE INTO work_refresh_state(
                       id, status, result_json, updated_at
                   ) VALUES (1, 'idle', '{}', ?)""",
                (now,),
            )
            conn.commit()
        _initialized_paths.add(path_key)


def stable_id(namespace: str, *parts: str | None) -> str:
    normalized = "\n".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(f"{namespace}\n{normalized}".encode()).hexdigest()[:24]
    return f"{namespace}_{digest}"


def settings() -> dict:
    init()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM work_ledger_settings WHERE id = 1").fetchone()
    return {
        "collection_enabled": bool(row["collection_enabled"]),
        "paused_at": row["paused_at"],
        "explicit_roots": json.loads(row["explicit_roots_json"]),
        "retention_days": row["retention_days"],
        "updated_at": row["updated_at"],
    }


def configure_collection(
    *,
    enabled: bool,
    explicit_roots: list[str] | None = None,
    retention_days: int | None = None,
    now: int | None = None,
) -> dict:
    init()
    timestamp = int(now if now is not None else time.time())
    current = settings()
    roots = current["explicit_roots"] if explicit_roots is None else sorted({str(Path(root).expanduser()) for root in explicit_roots})
    retention = current["retention_days"] if retention_days is None else max(1, int(retention_days))
    with closing(_connect()) as conn:
        conn.execute(
            """UPDATE work_ledger_settings
               SET collection_enabled = ?, paused_at = ?, explicit_roots_json = ?,
                   retention_days = ?, updated_at = ?
               WHERE id = 1""",
            (int(enabled), None if enabled else timestamp, json.dumps(roots), retention, timestamp),
        )
        conn.commit()
    return settings()


def validate_event_metadata(metadata: dict | None) -> dict:
    value = metadata or {}
    if not isinstance(value, dict):
        raise PrivacyBoundaryError("event metadata must be an object")
    disallowed = sorted(set(value) - ALLOWED_EVENT_METADATA)
    if disallowed:
        raise PrivacyBoundaryError(f"event metadata keys are not allowed: {', '.join(disallowed)}")
    return value


def _normalize_work_item(
    *,
    kind: str,
    name: str,
    parent_id: str | None,
    repository_id: str | None,
    color_hex: str | None,
) -> dict:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in WORK_ITEM_KINDS:
        raise ValueError("kind must be project, task, or story")
    normalized_name = " ".join(str(name).split())
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("name must contain 1 to 120 characters")
    normalized_color = color_hex.upper() if color_hex else None
    if normalized_color and not re.fullmatch(r"#[0-9A-F]{6}", normalized_color):
        raise ValueError("color_hex must use #RRGGBB")
    if normalized_kind == "project" and parent_id is not None:
        raise ValueError("projects cannot have a parent")
    if normalized_kind != "project" and not parent_id:
        raise ValueError("tasks and stories require a project parent")
    return {
        "kind": normalized_kind,
        "name": normalized_name,
        "parent_id": parent_id,
        "repository_id": repository_id,
        "color_hex": normalized_color,
    }


def create_work_item(
    *,
    kind: str,
    name: str,
    parent_id: str | None = None,
    repository_id: str | None = None,
    color_hex: str | None = None,
    now: int | None = None,
) -> dict:
    init()
    timestamp = int(now if now is not None else time.time())
    item = _normalize_work_item(
        kind=kind,
        name=name,
        parent_id=parent_id,
        repository_id=repository_id,
        color_hex=color_hex,
    )
    item_id = stable_id(
        "work_item",
        item["kind"],
        item["parent_id"],
        item["name"].casefold(),
    )
    with closing(_connect()) as conn:
        if item["parent_id"]:
            parent = conn.execute(
                """SELECT kind FROM work_items
                   WHERE id = ? AND archived_at IS NULL""",
                (item["parent_id"],),
            ).fetchone()
            if parent is None or parent["kind"] != "project":
                raise ValueError("parent_id must reference an active project")
        if item["repository_id"]:
            repository = conn.execute(
                "SELECT 1 FROM repositories WHERE id = ? AND archived_at IS NULL",
                (item["repository_id"],),
            ).fetchone()
            if repository is None:
                raise ValueError("repository_id must reference an active repository")
        conn.execute(
            """INSERT INTO work_items(
                   id, kind, parent_id, repository_id, name, color_hex,
                   archived_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   repository_id = excluded.repository_id,
                   color_hex = excluded.color_hex,
                   archived_at = NULL,
                   updated_at = excluded.updated_at""",
            (
                item_id,
                item["kind"],
                item["parent_id"],
                item["repository_id"],
                item["name"],
                item["color_hex"],
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return get_work_item(item_id)


def get_work_item(item_id: str) -> dict | None:
    init()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_work_items(*, include_archived: bool = False) -> list[dict]:
    init()
    where = "" if include_archived else "WHERE items.archived_at IS NULL"
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT items.*, parents.name AS parent_name,
                       COALESCE(items.repository_id, parents.repository_id)
                           AS effective_repository_id,
                       repositories.display_name AS repository_name
                FROM work_items AS items
                LEFT JOIN work_items AS parents ON parents.id = items.parent_id
                LEFT JOIN repositories
                    ON repositories.id = COALESCE(
                        items.repository_id,
                        parents.repository_id
                    )
                {where}
                ORDER BY CASE items.kind
                             WHEN 'project' THEN 0 WHEN 'story' THEN 1 ELSE 2 END,
                         COALESCE(parents.name, items.name) COLLATE NOCASE,
                         items.name COLLATE NOCASE"""
        ).fetchall()
    return [dict(row) for row in rows]


def active_work_context() -> dict:
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT intervals.id AS interval_id, intervals.started_at_us,
                      items.id AS work_item_id, items.kind, items.name,
                      items.parent_id, parents.name AS parent_name,
                      COALESCE(items.repository_id, parents.repository_id)
                          AS repository_id,
                      repositories.display_name AS repository_name,
                      COALESCE(items.color_hex, parents.color_hex) AS color_hex
               FROM work_attribution_intervals AS intervals
               JOIN work_items AS items ON items.id = intervals.work_item_id
               LEFT JOIN work_items AS parents ON parents.id = items.parent_id
               LEFT JOIN repositories
                   ON repositories.id = COALESCE(
                       items.repository_id,
                       parents.repository_id
                   )
               WHERE intervals.ended_at_us IS NULL
               LIMIT 1"""
        ).fetchone()
    return {"state": "active", **dict(row)} if row else {"state": "unassigned"}


def switch_active_work_item(
    work_item_id: str | None,
    *,
    at_us: int | None = None,
    now: int | None = None,
) -> dict:
    init()
    timestamp_us = int(at_us if at_us is not None else time.time_ns() // 1_000)
    created_at = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT id, work_item_id, started_at_us
               FROM work_attribution_intervals
               WHERE ended_at_us IS NULL LIMIT 1"""
        ).fetchone()
        if current and current["work_item_id"] == work_item_id:
            conn.commit()
            return active_work_context()
        if work_item_id is not None:
            target = conn.execute(
                "SELECT 1 FROM work_items WHERE id = ? AND archived_at IS NULL",
                (work_item_id,),
            ).fetchone()
            if target is None:
                conn.rollback()
                raise ValueError("work_item_id must reference an active work item")
        if current:
            ended_at_us = max(timestamp_us, int(current["started_at_us"]))
            conn.execute(
                "UPDATE work_attribution_intervals SET ended_at_us = ? WHERE id = ?",
                (ended_at_us, current["id"]),
            )
        if work_item_id is not None:
            interval_id = stable_id("work_interval", work_item_id, str(timestamp_us))
            conn.execute(
                """INSERT INTO work_attribution_intervals(
                       id, work_item_id, started_at_us, ended_at_us, source, created_at
                   ) VALUES (?, ?, ?, NULL, 'manual', ?)""",
                (interval_id, work_item_id, timestamp_us, created_at),
            )
        conn.commit()
    return active_work_context()


def archive_work_item(
    item_id: str,
    *,
    at_us: int | None = None,
    now: int | None = None,
) -> bool:
    init()
    timestamp = int(now if now is not None else time.time())
    timestamp_us = int(at_us if at_us is not None else time.time_ns() // 1_000)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [
            row["id"]
            for row in conn.execute(
                """WITH RECURSIVE descendants(id) AS (
                       SELECT id FROM work_items WHERE id = ?
                       UNION ALL
                       SELECT items.id FROM work_items AS items
                       JOIN descendants ON items.parent_id = descendants.id
                   ) SELECT id FROM descendants""",
                (item_id,),
            ).fetchall()
        ]
        if not ids:
            conn.rollback()
            return False
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE work_items SET archived_at = ?, updated_at = ? WHERE id IN ({placeholders})",
            (timestamp, timestamp, *ids),
        )
        conn.execute(
            f"""UPDATE work_attribution_intervals
                SET ended_at_us = MAX(started_at_us, ?)
                WHERE ended_at_us IS NULL AND work_item_id IN ({placeholders})""",
            (timestamp_us, *ids),
        )
        conn.execute(
            f"DELETE FROM session_work_assignments WHERE work_item_id IN ({placeholders})",
            ids,
        )
        conn.commit()
    return True


def list_attribution_intervals(*, limit: int = 500) -> list[dict]:
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT intervals.*, items.kind, items.name,
                      items.parent_id, parents.name AS parent_name
               FROM work_attribution_intervals AS intervals
               JOIN work_items AS items ON items.id = intervals.work_item_id
               LEFT JOIN work_items AS parents ON parents.id = items.parent_id
               ORDER BY intervals.started_at_us DESC
               LIMIT ?""",
            (max(1, min(int(limit), 5_000)),),
        ).fetchall()
    return [dict(row) for row in rows]


def reporting_snapshot(start_us: int, end_us: int) -> dict:
    """Read the bounded ledger rows needed for one private work report."""
    init()
    if end_us < start_us:
        raise ValueError("report end must be at or after start")
    with closing(_connect()) as conn:
        items = conn.execute(
            """SELECT items.*, parents.name AS parent_name,
                      COALESCE(items.repository_id, parents.repository_id)
                          AS effective_repository_id,
                      COALESCE(items.color_hex, parents.color_hex)
                          AS effective_color_hex,
                      repositories.display_name AS repository_name
               FROM work_items AS items
               LEFT JOIN work_items AS parents ON parents.id = items.parent_id
               LEFT JOIN repositories
                   ON repositories.id = COALESCE(
                       items.repository_id,
                       parents.repository_id
                   )
               ORDER BY items.created_at, items.id"""
        ).fetchall()
        intervals = conn.execute(
            """SELECT * FROM work_attribution_intervals
               WHERE started_at_us <= ?
                 AND COALESCE(ended_at_us, ?) >= ?
               ORDER BY started_at_us, id""",
            (end_us, end_us, start_us),
        ).fetchall()
        repositories = conn.execute(
            """SELECT id, display_name, common_dir, enabled, archived_at
               FROM repositories ORDER BY display_name COLLATE NOCASE, id"""
        ).fetchall()
        sessions = conn.execute(
            """SELECT id, provider, provider_session_id, repository_id,
                      worktree_id, branch, head_sha
               FROM ai_sessions WHERE archived_at IS NULL"""
        ).fetchall()
        session_assignments = conn.execute(
            """SELECT ai_session_id, assignment_mode, work_item_id
               FROM session_work_assignments"""
        ).fetchall()
        activities = conn.execute(
            """SELECT id, kind, occurred_at_us, repository_id, ai_session_id,
                      provider, source, metadata_json
               FROM activity_events
               WHERE archived_at IS NULL AND is_current = 1
                 AND occurred_at_us BETWEEN ? AND ?
               ORDER BY occurred_at_us, id""",
            (start_us, end_us),
        ).fetchall()
    return {
        "items": [dict(row) for row in items],
        "intervals": [dict(row) for row in intervals],
        "repositories": [
            {**dict(row), "enabled": bool(row["enabled"])}
            for row in repositories
        ],
        "sessions": [dict(row) for row in sessions],
        "session_assignments": [dict(row) for row in session_assignments],
        "activity_events": [
            {
                **{key: row[key] for key in row.keys() if key != "metadata_json"},
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in activities
        ],
    }


def session_cwds() -> list[str]:
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT cwd FROM ai_sessions
               WHERE archived_at IS NULL AND cwd IS NOT NULL AND cwd != ''
               ORDER BY cwd"""
        ).fetchall()
    return [str(row["cwd"]) for row in rows]


def refresh_state() -> dict:
    init()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM work_refresh_state WHERE id = 1").fetchone()
    return {
        **{key: row[key] for key in row.keys() if key != "result_json"},
        "result": json.loads(row["result_json"]),
    }


def update_refresh_state(
    status: str,
    *,
    started_at: int | None = None,
    finished_at: int | None = None,
    error: str | None = None,
    result: dict | None = None,
    now: int | None = None,
) -> dict:
    init()
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute(
            """UPDATE work_refresh_state
               SET status = ?,
                   started_at = COALESCE(?, started_at),
                   finished_at = ?,
                   last_error = ?,
                   result_json = ?,
                   updated_at = ?
               WHERE id = 1""",
            (
                status,
                started_at,
                finished_at,
                error,
                json.dumps(result or {}, sort_keys=True, separators=(",", ":")),
                timestamp,
            ),
        )
        conn.commit()
    return refresh_state()


def upsert_repository(record: dict, *, now: int | None = None) -> str:
    init()
    timestamp = int(now if now is not None else time.time())
    repository_id = str(record["id"])
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO repositories(
                   id, display_name, identity_kind, common_dir, remote_fingerprint,
                   root_commit_fingerprint, enabled, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   display_name = excluded.display_name,
                   identity_kind = excluded.identity_kind,
                   common_dir = excluded.common_dir,
                   remote_fingerprint = excluded.remote_fingerprint,
                   root_commit_fingerprint = excluded.root_commit_fingerprint,
                   last_seen_at = excluded.last_seen_at""",
            (
                repository_id,
                record["display_name"],
                record["identity_kind"],
                record["common_dir"],
                record.get("remote_fingerprint"),
                record.get("root_commit_fingerprint"),
                int(bool(record.get("enabled", False))),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return repository_id


def find_repository_id(
    *,
    common_dir: str,
    remote_fingerprint: str | None,
    root_commit_fingerprint: str | None,
) -> str | None:
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id FROM repositories WHERE common_dir = ? LIMIT 1",
            (common_dir,),
        ).fetchone()
        if row is None and remote_fingerprint:
            row = conn.execute(
                "SELECT id FROM repositories WHERE remote_fingerprint = ? ORDER BY last_seen_at DESC LIMIT 1",
                (remote_fingerprint,),
            ).fetchone()
        if row is None and root_commit_fingerprint:
            row = conn.execute(
                """SELECT id FROM repositories
                   WHERE remote_fingerprint IS NULL AND root_commit_fingerprint = ?
                   ORDER BY last_seen_at DESC LIMIT 1""",
                (root_commit_fingerprint,),
            ).fetchone()
    return str(row[0]) if row else None


def list_repositories(*, include_archived: bool = False) -> list[dict]:
    init()
    where = "" if include_archived else "WHERE archived_at IS NULL"
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT id, display_name, identity_kind, common_dir,
                       remote_fingerprint, root_commit_fingerprint, enabled,
                       archived_at, first_seen_at, last_seen_at
                FROM repositories {where}
                ORDER BY display_name COLLATE NOCASE, id"""
        ).fetchall()
    return [
        {
            "id": row["id"],
            "display_name": row["display_name"],
            "identity_kind": row["identity_kind"],
            "common_dir": row["common_dir"],
            "remote_fingerprint": row["remote_fingerprint"],
            "root_commit_fingerprint": row["root_commit_fingerprint"],
            "enabled": bool(row["enabled"]),
            "archived_at": row["archived_at"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def set_repository_enabled(repository_id: str, enabled: bool, *, now: int | None = None) -> bool:
    init()
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        cursor = conn.execute(
            "UPDATE repositories SET enabled = ?, last_seen_at = ? WHERE id = ?",
            (int(enabled), timestamp, repository_id),
        )
        conn.commit()
    return bool(cursor.rowcount)


def upsert_worktree(record: dict, *, now: int | None = None) -> str:
    init()
    timestamp = int(now if now is not None else time.time())
    worktree_id = str(record["id"])
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO repository_worktrees(
                   id, repository_id, worktree_key, path, git_dir, branch, head_sha,
                   is_present, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   path = excluded.path,
                   git_dir = excluded.git_dir,
                   branch = excluded.branch,
                   head_sha = excluded.head_sha,
                   is_present = excluded.is_present,
                   last_seen_at = excluded.last_seen_at""",
            (
                worktree_id,
                record["repository_id"],
                record["worktree_key"],
                record["path"],
                record["git_dir"],
                record.get("branch"),
                record.get("head_sha"),
                int(bool(record.get("is_present", True))),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return worktree_id


def upsert_ai_session(record: dict, *, now: int | None = None) -> str:
    init()
    provider = str(record["provider"]).strip().lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("work ledger sessions support only Claude and Codex")
    provider_session_id = str(record["provider_session_id"]).strip()
    if not provider_session_id:
        raise ValueError("provider_session_id is required")
    timestamp = int(now if now is not None else time.time())
    session_id = str(record.get("id") or stable_id("session", provider, provider_session_id))
    models = sorted({str(model) for model in record.get("models", []) if str(model).strip()})
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO ai_sessions(
                   id, provider, provider_session_id, source_file_key, source_path,
                   started_at_us, ended_at_us, cwd, repository_id, worktree_id,
                   branch, head_sha, native_title, source_modified_at_us,
                   models_json, messages, user_messages, requests,
                   input_tokens, output_tokens, cache_tokens, reasoning_tokens,
                   estimated_cost_usd, estimated_credits, source_health,
                   first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   source_file_key = excluded.source_file_key,
                   source_path = excluded.source_path,
                   started_at_us = excluded.started_at_us,
                   ended_at_us = excluded.ended_at_us,
                   cwd = excluded.cwd,
                   repository_id = excluded.repository_id,
                   worktree_id = excluded.worktree_id,
                   branch = excluded.branch,
                   head_sha = excluded.head_sha,
                   native_title = COALESCE(excluded.native_title, ai_sessions.native_title),
                   source_modified_at_us = excluded.source_modified_at_us,
                   models_json = excluded.models_json,
                   messages = excluded.messages,
                   user_messages = excluded.user_messages,
                   requests = excluded.requests,
                   input_tokens = excluded.input_tokens,
                   output_tokens = excluded.output_tokens,
                   cache_tokens = excluded.cache_tokens,
                   reasoning_tokens = excluded.reasoning_tokens,
                   estimated_cost_usd = excluded.estimated_cost_usd,
                   estimated_credits = excluded.estimated_credits,
                   source_health = excluded.source_health,
                   last_seen_at = excluded.last_seen_at""",
            (
                session_id,
                provider,
                provider_session_id,
                record.get("source_file_key"),
                record.get("source_path"),
                record.get("started_at_us"),
                record.get("ended_at_us"),
                record.get("cwd"),
                record.get("repository_id"),
                record.get("worktree_id"),
                record.get("branch"),
                record.get("head_sha"),
                record.get("native_title"),
                record.get("source_modified_at_us"),
                json.dumps(models, separators=(",", ":")),
                int(record.get("messages", 0) or 0),
                int(record.get("user_messages", 0) or 0),
                int(record.get("requests", 0) or 0),
                int(record.get("input_tokens", 0) or 0),
                int(record.get("output_tokens", 0) or 0),
                int(record.get("cache_tokens", 0) or 0),
                int(record.get("reasoning_tokens", 0) or 0),
                record.get("estimated_cost_usd"),
                record.get("estimated_credits"),
                str(record.get("source_health") or "current"),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return session_id


def upsert_session_presence(record: dict, *, now: int | None = None) -> str:
    """Upsert live provider metadata without replacing normalized usage totals."""
    init()
    provider = str(record["provider"]).strip().lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("work ledger sessions support only Claude and Codex")
    provider_session_id = str(record["provider_session_id"]).strip()
    if not provider_session_id:
        raise ValueError("provider_session_id is required")
    timestamp = int(now if now is not None else time.time())
    session_id = str(record.get("id") or stable_id("session", provider, provider_session_id))
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO ai_sessions(
                   id, provider, provider_session_id, started_at_us, ended_at_us,
                   cwd, repository_id, worktree_id, branch, head_sha,
                   native_title, source_modified_at_us, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   started_at_us = COALESCE(ai_sessions.started_at_us, excluded.started_at_us),
                   ended_at_us = MAX(
                       COALESCE(ai_sessions.ended_at_us, 0),
                       COALESCE(excluded.ended_at_us, 0)
                   ),
                   cwd = COALESCE(excluded.cwd, ai_sessions.cwd),
                   repository_id = COALESCE(excluded.repository_id, ai_sessions.repository_id),
                   worktree_id = COALESCE(excluded.worktree_id, ai_sessions.worktree_id),
                   branch = COALESCE(excluded.branch, ai_sessions.branch),
                   head_sha = COALESCE(excluded.head_sha, ai_sessions.head_sha),
                   native_title = COALESCE(excluded.native_title, ai_sessions.native_title),
                   source_modified_at_us = MAX(
                       COALESCE(ai_sessions.source_modified_at_us, 0),
                       COALESCE(excluded.source_modified_at_us, 0)
                   ),
                   last_seen_at = excluded.last_seen_at""",
            (
                session_id,
                provider,
                provider_session_id,
                record.get("started_at_us"),
                record.get("ended_at_us"),
                record.get("cwd"),
                record.get("repository_id"),
                record.get("worktree_id"),
                record.get("branch"),
                record.get("head_sha"),
                record.get("native_title"),
                record.get("source_modified_at_us"),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return session_id


def list_ai_sessions(
    *,
    provider: str | None = None,
    repository_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    init()
    clauses = ["sessions.archived_at IS NULL"]
    params: list[object] = []
    if provider:
        clauses.append("sessions.provider = ?")
        params.append(provider)
    if repository_id:
        clauses.append("sessions.repository_id = ?")
        params.append(repository_id)
    params.append(max(1, min(int(limit), 2_000)))
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT sessions.*,
                       labels.nickname,
                       labels.source AS nickname_source,
                       assignments.assignment_mode,
                       assignments.work_item_id AS assigned_work_item_id,
                       items.kind AS assigned_work_kind,
                       items.name AS assigned_work_name,
                       items.parent_id AS assigned_parent_id,
                       parents.name AS assigned_parent_name,
                       COALESCE(items.repository_id, parents.repository_id)
                           AS assigned_repository_id,
                       repositories.display_name AS assigned_repository_name,
                       COALESCE(items.color_hex, parents.color_hex) AS assigned_color_hex,
                       session_repositories.display_name AS repository_name,
                       worktrees.path AS worktree_path
                FROM ai_sessions AS sessions
                LEFT JOIN session_labels AS labels ON labels.ai_session_id = sessions.id
                LEFT JOIN session_work_assignments AS assignments
                    ON assignments.ai_session_id = sessions.id
                LEFT JOIN work_items AS items
                    ON items.id = assignments.work_item_id AND items.archived_at IS NULL
                LEFT JOIN work_items AS parents ON parents.id = items.parent_id
                LEFT JOIN repositories ON repositories.id = COALESCE(
                    items.repository_id,
                    parents.repository_id
                )
                LEFT JOIN repositories AS session_repositories
                    ON session_repositories.id = sessions.repository_id
                LEFT JOIN repository_worktrees AS worktrees
                    ON worktrees.id = sessions.worktree_id
                WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(
                             sessions.source_modified_at_us,
                             sessions.ended_at_us,
                             sessions.started_at_us,
                             0
                         ) DESC,
                         sessions.provider, sessions.provider_session_id
                LIMIT ?""",
            params,
        ).fetchall()
    return [
        {
            **{key: row[key] for key in row.keys() if key != "models_json"},
            "models": json.loads(row["models_json"]),
        }
        for row in rows
    ]


def set_session_nickname(
    ai_session_id: str,
    nickname: str | None,
    *,
    source: str = "usage_tracker",
    overwrite: bool = True,
    now: int | None = None,
) -> bool:
    init()
    normalized = " ".join(str(nickname or "").split())
    if len(normalized) > 80:
        raise ValueError("nickname must contain at most 80 characters")
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        if conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = ? AND archived_at IS NULL",
            (ai_session_id,),
        ).fetchone() is None:
            return False
        if not normalized:
            conn.execute("DELETE FROM session_labels WHERE ai_session_id = ?", (ai_session_id,))
        elif overwrite:
            conn.execute(
                """INSERT INTO session_labels(
                       ai_session_id, nickname, source, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ai_session_id) DO UPDATE SET
                       nickname = excluded.nickname,
                       source = excluded.source,
                       updated_at = excluded.updated_at""",
                (ai_session_id, normalized, source, timestamp, timestamp),
            )
        else:
            conn.execute(
                """INSERT OR IGNORE INTO session_labels(
                       ai_session_id, nickname, source, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (ai_session_id, normalized, source, timestamp, timestamp),
            )
        conn.commit()
    return True


def set_session_work_assignment(
    ai_session_id: str,
    *,
    mode: str,
    work_item_id: str | None = None,
    now: int | None = None,
) -> bool:
    init()
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in SESSION_ASSIGNMENT_MODES | {"automatic"}:
        raise ValueError("mode must be automatic, unassigned, or work_item")
    if normalized_mode == "work_item" and not work_item_id:
        raise ValueError("work_item mode requires work_item_id")
    if normalized_mode != "work_item" and work_item_id is not None:
        raise ValueError("work_item_id is only valid for work_item mode")
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        if conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = ? AND archived_at IS NULL",
            (ai_session_id,),
        ).fetchone() is None:
            return False
        if normalized_mode == "automatic":
            conn.execute(
                "DELETE FROM session_work_assignments WHERE ai_session_id = ?",
                (ai_session_id,),
            )
        else:
            if work_item_id and conn.execute(
                "SELECT 1 FROM work_items WHERE id = ? AND archived_at IS NULL",
                (work_item_id,),
            ).fetchone() is None:
                raise ValueError("work_item_id must reference an active work item")
            conn.execute(
                """INSERT INTO session_work_assignments(
                       ai_session_id, assignment_mode, work_item_id,
                       source, created_at, updated_at
                   ) VALUES (?, ?, ?, 'manual', ?, ?)
                   ON CONFLICT(ai_session_id) DO UPDATE SET
                       assignment_mode = excluded.assignment_mode,
                       work_item_id = excluded.work_item_id,
                       source = excluded.source,
                       updated_at = excluded.updated_at""",
                (ai_session_id, normalized_mode, work_item_id, timestamp, timestamp),
            )
        conn.commit()
    return True


def get_ai_session(provider: str, provider_session_id: str) -> dict | None:
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            """SELECT * FROM ai_sessions
               WHERE provider = ? AND provider_session_id = ?
                 AND archived_at IS NULL
               LIMIT 1""",
            (provider, provider_session_id),
        ).fetchone()
    if row is None:
        return None
    return {
        **{key: row[key] for key in row.keys() if key != "models_json"},
        "models": json.loads(row["models_json"]),
    }


def get_ai_session_by_id(session_id: str) -> dict | None:
    for session in list_ai_sessions(limit=2_000):
        if session["id"] == session_id:
            return session
    return None


def upsert_activity_event(record: dict, *, now: int | None = None) -> str:
    init()
    timestamp = int(now if now is not None else time.time())
    metadata = validate_event_metadata(record.get("metadata"))
    event_id = str(record.get("id") or stable_id("event", record["event_key"]))
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO activity_events(
                   id, event_key, kind, occurred_at_us, repository_id, worktree_id,
                   ai_session_id, provider, source, source_cursor, metadata_json,
                   is_current, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_key) DO UPDATE SET
                   occurred_at_us = excluded.occurred_at_us,
                   repository_id = excluded.repository_id,
                   worktree_id = excluded.worktree_id,
                   ai_session_id = excluded.ai_session_id,
                   provider = excluded.provider,
                   source_cursor = excluded.source_cursor,
                   metadata_json = excluded.metadata_json,
                   is_current = excluded.is_current,
                   last_seen_at = excluded.last_seen_at""",
            (
                event_id,
                record["event_key"],
                record["kind"],
                int(record["occurred_at_us"]),
                record.get("repository_id"),
                record.get("worktree_id"),
                record.get("ai_session_id"),
                record.get("provider"),
                record["source"],
                record.get("source_cursor"),
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                int(bool(record.get("is_current", True))),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return event_id


def git_index_state(worktree_id: str) -> dict | None:
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM git_index_state WHERE worktree_id = ?",
            (worktree_id,),
        ).fetchone()
    return dict(row) if row else None


def update_git_index_state(
    *,
    worktree_id: str,
    repository_id: str,
    head_sha: str | None,
    branch: str | None,
    dirty_hash: str | None,
    error: str | None = None,
    pull_request_index_version: int = 1,
    now: int | None = None,
) -> None:
    init()
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT INTO git_index_state(
                   worktree_id, repository_id, last_head_sha, last_branch,
                   dirty_hash, last_scan_at, last_error,
                   pull_request_index_version, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(worktree_id) DO UPDATE SET
                   repository_id = excluded.repository_id,
                   last_head_sha = excluded.last_head_sha,
                   last_branch = excluded.last_branch,
                   dirty_hash = excluded.dirty_hash,
                   last_scan_at = excluded.last_scan_at,
                   last_error = excluded.last_error,
                   pull_request_index_version = excluded.pull_request_index_version,
                   updated_at = excluded.updated_at""",
            (
                worktree_id,
                repository_id,
                head_sha,
                branch,
                dirty_hash,
                timestamp,
                error,
                int(pull_request_index_version),
                timestamp,
            ),
        )
        conn.commit()


def reconcile_current_git_commits(
    repository_id: str,
    current_shas: set[str],
    *,
    cutoff_us: int,
    now: int | None = None,
) -> int:
    init()
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute("CREATE TEMP TABLE current_git_commits(sha TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO current_git_commits(sha) VALUES (?)",
            ((sha,) for sha in current_shas),
        )
        cursor = conn.execute(
            """UPDATE activity_events
               SET is_current = 0, last_seen_at = ?
               WHERE repository_id = ?
                 AND kind IN ('git_commit', 'git_pull_request')
                 AND occurred_at_us >= ? AND is_current = 1
                 AND source_cursor NOT IN (SELECT sha FROM current_git_commits)""",
            (timestamp, repository_id, int(cutoff_us)),
        )
        changed = int(cursor.rowcount or 0)
        conn.commit()
    return changed


def set_activity_event_current(event_key: str, is_current: bool, *, now: int | None = None) -> bool:
    init()
    timestamp = int(now if now is not None else time.time())
    with closing(_connect()) as conn:
        cursor = conn.execute(
            "UPDATE activity_events SET is_current = ?, last_seen_at = ? WHERE event_key = ?",
            (int(is_current), timestamp, event_key),
        )
        conn.commit()
    return bool(cursor.rowcount)


def list_activity_events(
    *,
    repository_id: str | None = None,
    ai_session_id: str | None = None,
    provider: str | None = None,
    kind: str | None = None,
    include_superseded: bool = False,
    limit: int = 1_000,
) -> list[dict]:
    init()
    clauses = ["archived_at IS NULL"]
    params: list[object] = []
    if repository_id:
        clauses.append("repository_id = ?")
        params.append(repository_id)
    if ai_session_id:
        clauses.append("ai_session_id = ?")
        params.append(ai_session_id)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if not include_superseded:
        clauses.append("is_current = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 5_000)))
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""SELECT id, event_key, kind, occurred_at_us, repository_id,
                       worktree_id, ai_session_id, provider, source, source_cursor,
                       metadata_json, is_current, first_seen_at, last_seen_at
                FROM activity_events {where}
                ORDER BY occurred_at_us DESC, event_key
                LIMIT ?""",
            params,
        ).fetchall()
    return [
        {
            **{key: row[key] for key in row.keys() if key != "metadata_json"},
            "metadata": json.loads(row["metadata_json"]),
            "is_current": bool(row["is_current"]),
        }
        for row in rows
    ]


_TOTAL_FIELDS = (
    "messages",
    "user_messages",
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "reasoning_tokens",
)


def _totals(row: sqlite3.Row | None) -> dict:
    return {field: int((row[field] if row else 0) or 0) for field in _TOTAL_FIELDS}


def _subtract_totals(source: dict, indexed: dict) -> dict:
    return {field: source[field] - indexed[field] for field in _TOTAL_FIELDS}


def reconciliation() -> dict:
    """Compare normalized provider totals with repository attribution buckets."""
    init()
    providers: dict[str, dict] = {}
    with closing(_connect()) as conn:
        for provider in ("claude", "codex"):
            source = conn.execute(
                """SELECT
                       SUM(is_message) AS messages,
                       SUM(is_user) AS user_messages,
                       SUM(CASE WHEN input_tokens + output_tokens + cache_read_tokens
                                      + cache_write_5m_tokens + cache_write_1h_tokens
                                      + reasoning_tokens > 0
                                THEN 1 ELSE 0 END) AS requests,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_read_tokens + cache_write_5m_tokens
                           + cache_write_1h_tokens) AS cache_tokens,
                       SUM(reasoning_tokens) AS reasoning_tokens
                   FROM usage_events WHERE provider = ?""",
                (provider,),
            ).fetchone()
            indexed = conn.execute(
                """SELECT
                       SUM(messages) AS messages,
                       SUM(user_messages) AS user_messages,
                       SUM(requests) AS requests,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_tokens) AS cache_tokens,
                       SUM(reasoning_tokens) AS reasoning_tokens
                   FROM ai_sessions
                   WHERE provider = ? AND archived_at IS NULL""",
                (provider,),
            ).fetchone()
            bucket_rows = conn.execute(
                """SELECT
                       sessions.repository_id,
                       repositories.display_name,
                       COUNT(*) AS session_count,
                       SUM(sessions.messages) AS messages,
                       SUM(sessions.user_messages) AS user_messages,
                       SUM(sessions.requests) AS requests,
                       SUM(sessions.input_tokens) AS input_tokens,
                       SUM(sessions.output_tokens) AS output_tokens,
                       SUM(sessions.cache_tokens) AS cache_tokens,
                       SUM(sessions.reasoning_tokens) AS reasoning_tokens
                   FROM ai_sessions AS sessions
                   LEFT JOIN repositories ON repositories.id = sessions.repository_id
                   WHERE sessions.provider = ? AND sessions.archived_at IS NULL
                   GROUP BY sessions.repository_id, repositories.display_name
                   ORDER BY repositories.display_name COLLATE NOCASE""",
                (provider,),
            ).fetchall()
            source_totals = _totals(source)
            indexed_totals = _totals(indexed)
            buckets = [
                {
                    "bucket": "repository" if row["repository_id"] else "unassigned",
                    "repository_id": row["repository_id"],
                    "display_name": row["display_name"] or "Unassigned",
                    "session_count": int(row["session_count"] or 0),
                    "totals": _totals(row),
                }
                for row in bucket_rows
            ]
            if not any(bucket["bucket"] == "unassigned" for bucket in buckets):
                buckets.append({
                    "bucket": "unassigned",
                    "repository_id": None,
                    "display_name": "Unassigned",
                    "session_count": 0,
                    "totals": {field: 0 for field in _TOTAL_FIELDS},
                })
            providers[provider] = {
                "source_totals": source_totals,
                "indexed_session_totals": indexed_totals,
                "coverage_gap": _subtract_totals(source_totals, indexed_totals),
                "reconciled": source_totals == indexed_totals,
                "buckets": buckets,
            }
    return {"providers": providers}


def diagnostics() -> dict:
    state = settings()
    with closing(_connect()) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "repositories",
                "repository_worktrees",
                "ai_sessions",
                "activity_events",
                "work_items",
                "work_attribution_intervals",
                "session_labels",
                "session_work_assignments",
            )
        }
        schema_row = conn.execute(
            "SELECT value FROM work_ledger_meta WHERE key = 'schema_version'"
        ).fetchone()
        freshness = {
            "usage_by_provider": {
                row["provider"]: row["updated_at"]
                for row in conn.execute(
                    """SELECT provider, MAX(updated_at) AS updated_at
                       FROM usage_file_cursors GROUP BY provider"""
                ).fetchall()
            },
            "sessions": conn.execute(
                "SELECT MAX(last_seen_at) FROM ai_sessions"
            ).fetchone()[0],
            "activity_events": conn.execute(
                "SELECT MAX(last_seen_at) FROM activity_events"
            ).fetchone()[0],
            "git": conn.execute(
                "SELECT MAX(last_scan_at) FROM git_index_state"
            ).fetchone()[0],
            "provider_activity": {
                row["provider"]: row["updated_at"]
                for row in conn.execute(
                    """SELECT provider, MAX(updated_at) AS updated_at
                       FROM provider_activity_cursors GROUP BY provider"""
                ).fetchall()
            },
        }
    return {
        "schema_version": int(schema_row[0]),
        "settings": state,
        "counts": counts,
        "freshness": freshness,
        "active_work": active_work_context(),
        "refresh": refresh_state(),
    }
