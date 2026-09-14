"""Incremental rollup of normalized provider events into work sessions."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from src import repository_discovery, usage_ledger, work_ledger


_CONTEXT_READ_BYTES = 64 * 1024
_context_cache: dict[tuple[str, str, str], tuple[int, int, dict]] = {}


def _codex_thread_titles() -> dict[str, str]:
    """Read Codex's persisted thread names without opening session transcripts."""
    path = Path.home() / ".codex" / "state_5.sqlite"
    if not path.exists():
        return {}
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(threads)").fetchall()
            }
            if not {"id", "title"}.issubset(columns):
                return {}
            title_expression = (
                "COALESCE(NULLIF(TRIM(name), ''), title)"
                if "name" in columns
                else "title"
            )
            rows = conn.execute(
                f"SELECT id, {title_expression} FROM threads"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {
        str(session_id): " ".join(str(title).split())[:120]
        for session_id, title in rows
        if session_id and title and str(title).strip()
    }


def _claude_custom_title(source_path: str | None, session_id: str) -> str | None:
    """Incrementally scan only Claude custom-title records and persist the cursor."""
    if not source_path:
        return None
    path = Path(source_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    with closing(work_ledger._connect()) as conn:
        previous = conn.execute(
            "SELECT * FROM provider_title_cursors WHERE provider = ? AND path = ?",
            ("claude", str(path)),
        ).fetchone()
    reusable = bool(
        previous
        and previous["device"] == stat.st_dev
        and previous["inode"] == stat.st_ino
        and stat.st_size >= previous["offset_bytes"]
        and previous["cursor_hash"]
        and usage_ledger._cursor_hash(path, int(previous["offset_bytes"]))
        == previous["cursor_hash"]
    )
    offset = int(previous["offset_bytes"]) if reusable else 0
    title = str(previous["native_title"]) if reusable and previous["native_title"] else None
    if stat.st_size == offset:
        return title

    next_offset = offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    next_offset = handle.tell()
                    break
                if not raw_line.endswith(b"\n"):
                    next_offset = line_offset
                    break
                next_offset = handle.tell()
                if b'"customTitle"' not in raw_line:
                    continue
                try:
                    entry = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                entry_session = entry.get("sessionId")
                if entry_session and str(entry_session) != session_id:
                    continue
                custom_title = entry.get("customTitle")
                if isinstance(custom_title, str) and custom_title.strip():
                    title = " ".join(custom_title.split())[:120]
    except OSError:
        return title

    with closing(work_ledger._connect()) as conn:
        conn.execute(
            """INSERT INTO provider_title_cursors(
                   provider, path, device, inode, offset_bytes, size_bytes,
                   cursor_hash, native_title, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, path) DO UPDATE SET
                   device = excluded.device,
                   inode = excluded.inode,
                   offset_bytes = excluded.offset_bytes,
                   size_bytes = excluded.size_bytes,
                   cursor_hash = excluded.cursor_hash,
                   native_title = excluded.native_title,
                   updated_at = excluded.updated_at""",
            (
                "claude",
                str(path),
                stat.st_dev,
                stat.st_ino,
                next_offset,
                stat.st_size,
                usage_ledger._cursor_hash(path, next_offset) or "",
                title,
                int(time.time()),
            ),
        )
        conn.commit()
    return title


def _bounded_entries(path: Path) -> list[dict]:
    """Read session metadata from bounded file edges, never full transcripts."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(_CONTEXT_READ_BYTES)
            tail = b""
            if size > _CONTEXT_READ_BYTES:
                handle.seek(max(_CONTEXT_READ_BYTES, size - _CONTEXT_READ_BYTES))
                if handle.tell() > 0:
                    handle.readline()
                tail = handle.read(_CONTEXT_READ_BYTES)
    except OSError:
        return []

    entries = []
    for raw_line in (head + b"\n" + tail).splitlines():
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _source_context(provider: str, source_path: str | None, session_id: str) -> dict:
    context = {
        "cwd": None,
        "branch": None,
        "head_sha": None,
        "native_title": None,
        "source_modified_at_us": None,
    }
    if not source_path:
        return context
    path = Path(source_path)
    try:
        stat = path.stat()
    except OSError:
        return context
    context["source_modified_at_us"] = stat.st_mtime_ns // 1_000
    cache_key = (provider, source_path, session_id)
    cached = _context_cache.get(cache_key)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return dict(cached[2])

    for entry in _bounded_entries(path):
        if provider == "claude":
            entry_session = entry.get("sessionId")
            if entry_session and str(entry_session) != session_id:
                continue
            if isinstance(entry.get("cwd"), str) and entry["cwd"].strip():
                context["cwd"] = entry["cwd"].strip()
            if isinstance(entry.get("gitBranch"), str) and entry["gitBranch"].strip():
                context["branch"] = entry["gitBranch"].strip()[:240]
            if isinstance(entry.get("customTitle"), str) and entry["customTitle"].strip():
                context["native_title"] = entry["customTitle"].strip()[:120]
            elif (
                context["native_title"] is None
                and isinstance(entry.get("slug"), str)
                and entry["slug"].strip()
            ):
                context["native_title"] = entry["slug"].strip()[:120]
            continue

        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        payload_id = payload.get("id") or payload.get("session_id")
        if payload_id and str(payload_id) != session_id:
            continue
        if isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
            context["cwd"] = payload["cwd"].strip()
        for title_key in ("title", "name", "session_name"):
            title = payload.get(title_key)
            if isinstance(title, str) and title.strip():
                context["native_title"] = title.strip()[:120]
                break
        git_context = payload.get("git") or {}
        if isinstance(git_context, dict):
            if (
                isinstance(git_context.get("branch"), str)
                and git_context["branch"].strip()
            ):
                context["branch"] = git_context["branch"].strip()[:240]
            if (
                isinstance(git_context.get("commit_hash"), str)
                and git_context["commit_hash"].strip()
            ):
                context["head_sha"] = git_context["commit_hash"].strip()[:64]
    _context_cache[cache_key] = (stat.st_size, stat.st_mtime_ns, dict(context))
    return context


def _session_rows(provider: str, session_ids: set[str] | None) -> list[dict]:
    usage_ledger.init()
    clauses = ["provider = ?"]
    params: list[object] = [provider]
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        clauses.append(f"session_id IN ({placeholders})")
        params.extend(sorted(session_ids))
    with closing(usage_ledger._connect()) as conn:
        rows = conn.execute(
            f"""SELECT
                    provider,
                    session_id,
                    MIN(timestamp_us) AS started_at_us,
                    MAX(timestamp_us) AS ended_at_us,
                    MAX(NULLIF(cwd, '')) AS cwd,
                    MIN(path) AS source_path,
                    MIN(file_key) AS source_file_key,
                    GROUP_CONCAT(DISTINCT model) AS models,
                    SUM(is_message) AS messages,
                    SUM(is_user) AS user_messages,
                    SUM(CASE WHEN input_tokens + output_tokens + cache_read_tokens
                                      + cache_write_5m_tokens + cache_write_1h_tokens
                                      + reasoning_tokens > 0
                             THEN 1 ELSE 0 END) AS requests,
                    SUM(input_tokens) AS input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    SUM(cache_read_tokens + cache_write_5m_tokens + cache_write_1h_tokens) AS cache_tokens,
                    SUM(reasoning_tokens) AS reasoning_tokens
                FROM usage_events
                WHERE {' AND '.join(clauses)}
                GROUP BY provider, session_id""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def sync_normalized_sessions(provider: str, session_ids: set[str] | None = None) -> dict:
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider: {provider}")
    if not work_ledger.settings()["collection_enabled"]:
        return {"provider": provider, "status": "paused", "sessions": 0}

    rows = _session_rows(provider, session_ids)
    codex_titles = _codex_thread_titles() if provider == "codex" else {}
    repositories_by_cwd: dict[str, dict | None] = {}
    for row in rows:
        session_id = str(row["session_id"])
        source_context = _source_context(
            provider,
            row.get("source_path"),
            session_id,
        )
        claude_title = (
            _claude_custom_title(row.get("source_path"), session_id)
            if provider == "claude"
            else None
        )
        cwd = source_context.get("cwd") or row.get("cwd")
        if cwd and cwd not in repositories_by_cwd:
            repositories_by_cwd[cwd] = repository_discovery.discover_session_repository(
                cwd, enabled=False
            )
        repository = repositories_by_cwd.get(cwd) if cwd else None
        work_ledger.upsert_ai_session({
            "provider": provider,
            "provider_session_id": row["session_id"],
            "source_file_key": row["source_file_key"],
            "source_path": row["source_path"],
            "started_at_us": row["started_at_us"],
            "ended_at_us": row["ended_at_us"],
            "cwd": cwd,
            "repository_id": repository.get("repository_id") if repository else None,
            "worktree_id": repository.get("worktree_id") if repository else None,
            "branch": source_context.get("branch") or (
                repository.get("branch") if repository else None
            ),
            "head_sha": source_context.get("head_sha") or (
                repository.get("head_sha") if repository else None
            ),
            "native_title": codex_titles.get(session_id)
            or claude_title
            or source_context.get("native_title"),
            "source_modified_at_us": source_context.get("source_modified_at_us"),
            "models": (row.get("models") or "").split(","),
            "messages": row["messages"],
            "user_messages": row["user_messages"],
            "requests": row["requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_tokens": row["cache_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
        })
    return {"provider": provider, "status": "ok", "sessions": len(rows)}
