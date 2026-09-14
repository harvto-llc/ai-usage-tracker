"""Opt-in, privacy-bounded Claude and Codex tool activity indexing."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src import usage_ledger, work_ledger, work_session_sync


_TEST_PATTERNS = (
    r"\bpytest\b",
    r"\bunittest\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
    r"\b(?:cargo|go|swift)\s+test\b",
    r"\b(?:jest|vitest|rspec)\b",
    r"\bxcodebuild\b[^\n]*\btest\b",
)
_BUILD_PATTERNS = (
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b",
    r"\b(?:cargo|go|swift)\s+build\b",
    r"\bxcodebuild\b",
    r"\b(?:cmake|make|ninja)\b",
)
_EDIT_TOOLS = {"edit", "write", "notebookedit", "apply_patch"}
_COMMAND_TOOLS = {"bash", "exec", "exec_command", "shell"}


def _command_category(command: str | None) -> str:
    value = command or ""
    if any(re.search(pattern, value, re.IGNORECASE) for pattern in _TEST_PATTERNS):
        return "test"
    if any(re.search(pattern, value, re.IGNORECASE) for pattern in _BUILD_PATTERNS):
        return "build"
    if re.search(r"(?:^|[;&|]\s*)git(?:\s|$)", value, re.IGNORECASE):
        return "git"
    return "tool"


def _safe_tool_name(name: object) -> str:
    value = str(name or "tool").strip()[:100]
    if value.lower().startswith("mcp__"):
        return "MCP"
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)
    return cleaned or "tool"


def _descriptor(
    name: object,
    command: str | None = None,
    source_id: object = None,
) -> dict:
    tool_name = _safe_tool_name(name)
    normalized = tool_name.lower()
    if normalized in _EDIT_TOOLS:
        kind = "edit"
        category = "edit"
    elif normalized in _COMMAND_TOOLS:
        category = _command_category(command)
        kind = category
    else:
        kind = "tool"
        category = "tool"
    descriptor = {
        "kind": kind,
        "metadata": {"tool_name": tool_name, "command_category": category},
    }
    if source_id:
        descriptor["source_key"] = work_ledger.stable_id("tool_call", str(source_id))
    return descriptor


def _json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def extract_activity(provider: str, entry: dict) -> list[dict]:
    """Return privacy-safe activity descriptors; never return payload bodies."""
    if provider == "claude":
        message = entry.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        descriptors = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input") or {}
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            descriptors.append(_descriptor(block.get("name"), command, block.get("id")))
        return descriptors

    if provider != "codex" or entry.get("type") != "response_item":
        return []
    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return []
    payload_type = payload.get("type")
    if payload_type not in {"function_call", "custom_tool_call", "web_search_call"}:
        return []
    name = payload.get("name") or (
        "web_search" if payload_type == "web_search_call" else None
    )
    raw_arguments = (
        payload.get("arguments")
        if payload_type == "function_call"
        else payload.get("input")
    )
    arguments = _json_object(raw_arguments)
    command = arguments.get("cmd") or arguments.get("command")
    if not command and isinstance(raw_arguments, str) and name in {"exec", "exec_command"}:
        command = raw_arguments
    return [_descriptor(
        name,
        str(command) if command is not None else None,
        payload.get("call_id") or payload.get("id"),
    )]


def _cursor(
    conn: sqlite3.Connection,
    provider: str,
    path: Path,
    stat: os.stat_result,
) -> sqlite3.Row | None:
    direct = conn.execute(
        "SELECT * FROM provider_activity_cursors WHERE provider = ? AND path = ?",
        (provider, str(path)),
    ).fetchone()
    if direct is not None:
        return direct
    return conn.execute(
        """SELECT * FROM provider_activity_cursors
           WHERE provider = ? AND device = ? AND inode = ?
           ORDER BY updated_at DESC LIMIT 1""",
        (provider, stat.st_dev, stat.st_ino),
    ).fetchone()


def _update_state(provider: str, entry: dict, state: dict) -> None:
    if provider == "claude":
        if entry.get("sessionId"):
            state["session_id"] = str(entry["sessionId"])
        if isinstance(entry.get("cwd"), str) and entry["cwd"].strip():
            state["cwd"] = entry["cwd"].strip()
        return
    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return
    if entry.get("type") == "session_meta" and payload.get("id"):
        state["session_id"] = str(payload["id"])
    if isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
        state["cwd"] = payload["cwd"].strip()


def _context(provider: str, state: dict, cache: dict[str, dict]) -> dict:
    session_id = state["session_id"]
    if session_id in cache:
        return cache[session_id]
    session = work_ledger.get_ai_session(provider, session_id)
    if session:
        context = {
            "ai_session_id": session["id"],
            "repository_id": session["repository_id"],
            "worktree_id": session["worktree_id"],
        }
    else:
        context = {
            "ai_session_id": None,
            "repository_id": None,
            "worktree_id": None,
        }
    cache[session_id] = context
    return context


def _index_file(provider: str, path: Path, cutoff_us: int, now: int) -> int:
    try:
        stat = path.stat()
    except OSError:
        return 0
    work_ledger.init()
    with closing(work_ledger._connect()) as conn:
        previous = _cursor(conn, provider, path, stat)
        reusable = bool(
            previous
            and previous["device"] == stat.st_dev
            and previous["inode"] == stat.st_ino
            and stat.st_size >= previous["offset_bytes"]
            and previous["cursor_hash"]
            and usage_ledger._cursor_hash(path, int(previous["offset_bytes"]))
            == previous["cursor_hash"]
        )
        file_key = str(previous["file_key"]) if reusable else usage_ledger._file_key(stat)
        offset = int(previous["offset_bytes"]) if reusable else 0
        state = {
            "session_id": str(previous["session_id"] or path) if reusable else str(path),
            "cwd": str(previous["cwd"]) if reusable and previous["cwd"] else None,
        }
        if stat.st_size == offset:
            return 0

        inserted = 0
        next_offset = offset
        context_cache: dict[str, dict] = {}
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
                    try:
                        entry = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    _update_state(provider, entry, state)
                    at = usage_ledger._timestamp(entry.get("timestamp"))
                    if at is None:
                        continue
                    occurred_at_us = round(at.timestamp() * 1_000_000)
                    if occurred_at_us < cutoff_us:
                        continue
                    for index, descriptor in enumerate(extract_activity(provider, entry)):
                        context = _context(provider, state, context_cache)
                        source_key = descriptor.get("source_key") or work_ledger.stable_id(
                            "tool_offset",
                            file_key,
                            str(line_offset),
                            str(index),
                        )
                        event_key = f"{provider}:{state['session_id']}:{source_key}"
                        existed = conn.execute(
                            "SELECT 1 FROM activity_events WHERE event_key = ?",
                            (event_key,),
                        ).fetchone() is not None
                        metadata = work_ledger.validate_event_metadata(descriptor["metadata"])
                        conn.execute(
                            """INSERT INTO activity_events(
                                   id, event_key, kind, occurred_at_us, repository_id,
                                   worktree_id, ai_session_id, provider, source,
                                   source_cursor, metadata_json, is_current,
                                   first_seen_at, last_seen_at
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                               ON CONFLICT(event_key) DO UPDATE SET
                                   occurred_at_us = excluded.occurred_at_us,
                                   repository_id = excluded.repository_id,
                                   worktree_id = excluded.worktree_id,
                                   ai_session_id = excluded.ai_session_id,
                                   metadata_json = excluded.metadata_json,
                                   archived_at = NULL,
                                   last_seen_at = excluded.last_seen_at""",
                            (
                                work_ledger.stable_id("event", event_key),
                                event_key,
                                descriptor["kind"],
                                occurred_at_us,
                                context["repository_id"],
                                context["worktree_id"],
                                context["ai_session_id"],
                                provider,
                                "provider_session",
                                source_key,
                                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                                now,
                                now,
                            ),
                        )
                        if not existed:
                            inserted += 1
        except OSError:
            return 0

        conn.execute(
            """INSERT INTO provider_activity_cursors(
                   provider, path, file_key, device, inode, offset_bytes,
                   size_bytes, session_id, cwd, cursor_hash, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, path) DO UPDATE SET
                   file_key = excluded.file_key,
                   device = excluded.device,
                   inode = excluded.inode,
                   offset_bytes = excluded.offset_bytes,
                   size_bytes = excluded.size_bytes,
                   session_id = excluded.session_id,
                   cwd = excluded.cwd,
                   cursor_hash = excluded.cursor_hash,
                   updated_at = excluded.updated_at""",
            (
                provider,
                str(path),
                file_key,
                stat.st_dev,
                stat.st_ino,
                next_offset,
                stat.st_size,
                state["session_id"],
                state.get("cwd"),
                usage_ledger._cursor_hash(path, next_offset) or "",
                now,
            ),
        )
        conn.commit()
    return inserted


def sync_provider_activity(provider: str, *, sync_sessions: bool = True) -> dict:
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider: {provider}")
    settings = work_ledger.settings()
    if not settings["collection_enabled"]:
        return {"provider": provider, "status": "paused", "events_inserted": 0}
    if sync_sessions:
        work_session_sync.sync_normalized_sessions(provider)
    now = int(time.time())
    cutoff_us = max(0, now - settings["retention_days"] * 86400) * 1_000_000
    inserted = sum(
        _index_file(provider, Path(path), cutoff_us, now)
        for path in usage_ledger._provider_files(provider)
    )
    with closing(work_ledger._connect()) as conn:
        conn.execute(
            """UPDATE activity_events
               SET archived_at = ?, last_seen_at = ?
               WHERE provider = ? AND source = 'provider_session'
                 AND occurred_at_us < ? AND archived_at IS NULL""",
            (now, now, provider, cutoff_us),
        )
        conn.commit()
    return {"provider": provider, "status": "ok", "events_inserted": inserted}
