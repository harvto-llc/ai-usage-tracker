"""Incremental local index for Claude and Codex usage events.

The provider JSONL files remain authoritative. This module records the last
complete byte consumed from each file and persists normalized event metadata so
quota and activity windows can be queried without rereading full histories.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def _db_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_ACTIVITY_DB")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".usage-tracker" / "activity.db"
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    path_key = str(_db_path())
    if path_key in _initialized_paths:
        return
    with _init_lock:
        if path_key in _initialized_paths:
            return
        with closing(_connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_file_cursors(
                provider TEXT NOT NULL,
                path TEXT NOT NULL,
                file_key TEXT NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                offset_bytes INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                current_model TEXT NOT NULL DEFAULT 'unknown',
                current_surface TEXT NOT NULL DEFAULT 'unknown',
                session_id TEXT,
                cwd TEXT,
                parser_state_json TEXT NOT NULL DEFAULT '{}',
                cursor_hash TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(provider, path)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_file_cursors_key
            ON usage_file_cursors(provider, file_key);

            CREATE TABLE IF NOT EXISTS usage_events(
                provider TEXT NOT NULL,
                file_key TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                path TEXT NOT NULL,
                timestamp_us INTEGER NOT NULL,
                day TEXT NOT NULL,
                session_id TEXT NOT NULL,
                cwd TEXT,
                model TEXT NOT NULL,
                surface TEXT NOT NULL DEFAULT 'unknown',
                is_message INTEGER NOT NULL DEFAULT 0,
                is_user INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,
                usage_breakdown_json TEXT NOT NULL DEFAULT '{}',
                compaction_generation INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(provider, file_key, line_offset)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_events_provider_ts
            ON usage_events(provider, timestamp_us);

            CREATE INDEX IF NOT EXISTS idx_usage_events_provider_day_model
            ON usage_events(provider, day, model);

            CREATE TABLE IF NOT EXISTS usage_request_ids(
                provider TEXT NOT NULL,
                file_key TEXT NOT NULL,
                request_id TEXT NOT NULL,
                PRIMARY KEY(provider, file_key, request_id)
            );

            CREATE TABLE IF NOT EXISTS usage_ledger_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_daily_models(
                provider TEXT NOT NULL,
                day TEXT NOT NULL,
                model TEXT NOT NULL,
                messages INTEGER NOT NULL,
                user_messages INTEGER NOT NULL,
                sessions INTEGER NOT NULL,
                requests INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER NOT NULL,
                cache_write_5m_tokens INTEGER NOT NULL,
                cache_write_1h_tokens INTEGER NOT NULL,
                reasoning_tokens INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(provider, day, model)
                );

            -- The primary key is (provider, day, model), so `WHERE day = ?` cannot use
            -- it: `day` is not the leftmost column, and SQLite answers that predicate
            -- with a full SCAN of the autoindex. That is the query the 60s emission
            -- cycle runs to find today's providers, so "one day wide" was true of the
            -- PREDICATE and false of the WORK - it read every row of history, 1440
            -- times a day, to publish one day. Measured before this index existed:
            --   SCAN usage_daily_models USING COVERING INDEX sqlite_autoindex_usage_daily_models_1
            -- and after it:
            --   SEARCH usage_daily_models USING COVERING INDEX idx_usage_daily_models_day (day=?)
            -- `provider` is the second column so the lookup stays covering and the
            -- DISTINCT comes out of the index in order.
            CREATE INDEX IF NOT EXISTS idx_usage_daily_models_day
                ON usage_daily_models(day, provider);
                """
            )
            cursor_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(usage_file_cursors)").fetchall()
            }
            if "cursor_hash" not in cursor_columns:
                conn.execute(
                    "ALTER TABLE usage_file_cursors ADD COLUMN cursor_hash TEXT NOT NULL DEFAULT ''"
                )
            if "cwd" not in cursor_columns:
                conn.execute("ALTER TABLE usage_file_cursors ADD COLUMN cwd TEXT")
            explanation_migration = False
            if "parser_state_json" not in cursor_columns:
                conn.execute(
                    "ALTER TABLE usage_file_cursors ADD COLUMN parser_state_json TEXT NOT NULL DEFAULT '{}'"
                )
                explanation_migration = True
            surface_migration = False
            if "current_surface" not in cursor_columns:
                conn.execute(
                    "ALTER TABLE usage_file_cursors ADD COLUMN current_surface TEXT NOT NULL DEFAULT 'unknown'"
                )
                surface_migration = True
            event_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()
            }
            if "cwd" not in event_columns:
                conn.execute("ALTER TABLE usage_events ADD COLUMN cwd TEXT")
            if "surface" not in event_columns:
                conn.execute(
                    "ALTER TABLE usage_events ADD COLUMN surface TEXT NOT NULL DEFAULT 'unknown'"
                )
                surface_migration = True
            if "usage_breakdown_json" not in event_columns:
                conn.execute(
                    "ALTER TABLE usage_events ADD COLUMN usage_breakdown_json TEXT NOT NULL DEFAULT '{}'"
                )
                explanation_migration = True
            if "compaction_generation" not in event_columns:
                conn.execute(
                    "ALTER TABLE usage_events ADD COLUMN compaction_generation INTEGER NOT NULL DEFAULT 0"
                )
                explanation_migration = True
            explanation_backfill = conn.execute(
                "SELECT value FROM usage_ledger_meta WHERE key = 'usage_explanation_v2'"
            ).fetchone()
            if explanation_migration or explanation_backfill is None:
                conn.execute("DELETE FROM usage_file_cursors WHERE provider IN ('claude', 'codex')")
                conn.execute("DELETE FROM usage_events WHERE provider IN ('claude', 'codex')")
                conn.execute("DELETE FROM usage_daily_models WHERE provider IN ('claude', 'codex')")
                conn.execute("DELETE FROM usage_request_ids WHERE provider IN ('claude', 'codex')")
                conn.execute(
                    """INSERT OR REPLACE INTO usage_ledger_meta(key, value)
                       VALUES ('usage_explanation_v2', '1')"""
                )
            attribution_backfill = conn.execute(
                "SELECT value FROM usage_ledger_meta WHERE key = 'codex_attribution_backfill_v2'"
            ).fetchone()
            if surface_migration or attribution_backfill is None:
                conn.execute("DELETE FROM usage_file_cursors WHERE provider = 'codex'")
                conn.execute("DELETE FROM usage_events WHERE provider = 'codex'")
                conn.execute("DELETE FROM usage_daily_models WHERE provider = 'codex'")
                conn.execute("DELETE FROM usage_request_ids WHERE provider = 'codex'")
                conn.execute(
                    """INSERT OR REPLACE INTO usage_ledger_meta(key, value)
                       VALUES ('codex_surface_backfill_v1', '1')"""
                )
                conn.execute(
                    """INSERT OR REPLACE INTO usage_ledger_meta(key, value)
                       VALUES ('codex_attribution_backfill_v2', '1')"""
                )
            claude_backfill = conn.execute(
                "SELECT value FROM usage_ledger_meta WHERE key = 'claude_attribution_dedupe_v1'"
            ).fetchone()
            if surface_migration or claude_backfill is None:
                conn.execute("DELETE FROM usage_file_cursors WHERE provider = 'claude'")
                conn.execute("DELETE FROM usage_events WHERE provider = 'claude'")
                conn.execute("DELETE FROM usage_daily_models WHERE provider = 'claude'")
                conn.execute("DELETE FROM usage_request_ids WHERE provider = 'claude'")
                conn.execute(
                    """INSERT OR REPLACE INTO usage_ledger_meta(key, value)
                       VALUES ('claude_attribution_dedupe_v1', '1')"""
                )
            request_backfill = conn.execute(
                "SELECT value FROM usage_ledger_meta WHERE key = 'request_ids_backfilled'"
            ).fetchone()
            if request_backfill is None:
                conn.execute(
                    """INSERT OR IGNORE INTO usage_request_ids(provider, file_key, request_id)
                       SELECT provider, file_key, request_id FROM usage_events
                       WHERE request_id IS NOT NULL"""
                )
                conn.execute(
                    """INSERT INTO usage_ledger_meta(key, value)
                       VALUES ('request_ids_backfilled', '1')"""
                )
            conn.commit()
        _initialized_paths.add(path_key)


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event(
    *,
    at: datetime,
    session_id: str,
    cwd: str | None,
    model: str,
    surface: str,
    is_message: bool,
    is_user: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
    request_id: str | None = None,
    usage_breakdown: dict | None = None,
    compaction_generation: int = 0,
) -> dict:
    return {
        "timestamp_us": round(at.timestamp() * 1_000_000),
        "day": at.astimezone().strftime("%Y-%m-%d"),
        "session_id": session_id,
        "cwd": cwd,
        "model": model or "unknown",
        "surface": surface or "unknown",
        "is_message": int(is_message),
        "is_user": int(is_user),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_write_5m_tokens": int(cache_write_5m_tokens or 0),
        "cache_write_1h_tokens": int(cache_write_1h_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "request_id": request_id,
        "usage_breakdown": usage_breakdown or {},
        "compaction_generation": int(compaction_generation or 0),
    }


def _parse_claude_entry(entry: dict, state: dict) -> dict | None:
    from src.claude_sources import claude_event_identity
    from src.scanners import _countable_claude_message

    from src.usage_explanation import allocate_usage, observe_claude_entry

    at = _timestamp(entry.get("timestamp"))
    if at is None:
        return None
    message = entry.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    countable, role = _countable_claude_message(entry)
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    observe_claude_entry(entry, state, before_usage=True)
    session_id = str(entry.get("sessionId") or state["session_id"])
    state["session_id"] = session_id
    if isinstance(entry.get("cwd"), str) and entry["cwd"].strip():
        state["cwd"] = entry["cwd"].strip()

    cache_creation = usage.get("cache_creation") or {}
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
    write_total = int(usage.get("cache_creation_input_tokens") or 0)
    if not write_5m and not write_1h:
        write_5m = write_total
    elif write_total > write_5m + write_1h:
        write_5m += write_total - write_5m - write_1h

    token_total = sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens")
    ) + write_5m + write_1h
    if not countable and token_total <= 0:
        observe_claude_entry(entry, state, before_usage=False)
        return None
    request_id = claude_event_identity(entry)
    breakdown = (
        allocate_usage(
            state,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
        )
        if token_total > 0
        else {}
    )
    event = _event(
        at=at,
        session_id=session_id,
        cwd=state.get("cwd"),
        model=str(message.get("model") or "unknown"),
        surface=state["current_surface"],
        is_message=countable,
        is_user=role == "user",
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        request_id=request_id,
        usage_breakdown=breakdown,
        compaction_generation=int(state.get("compaction_generation") or 0),
    )
    observe_claude_entry(entry, state, before_usage=False)
    return event


def _parse_codex_entry(entry: dict, state: dict) -> dict | None:
    from src.codex_sources import canonical_codex_surface
    from src.provider_metrics import (
        CODEX_BOOKKEEPING_TYPES,
        _is_codex_message_payload,
        _is_codex_user_payload,
    )
    from src.usage_explanation import allocate_usage, observe_codex_entry

    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if entry.get("type") == "session_meta":
        if payload.get("id"):
            state["session_id"] = str(payload["id"])
        state["current_surface"] = canonical_codex_surface(
            payload.get("originator"), payload.get("source")
        )
    if isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
        state["cwd"] = payload["cwd"].strip()
    if isinstance(payload.get("model"), str) and payload["model"]:
        state["current_model"] = payload["model"]

    is_usage_entry = False
    info = payload.get("info") or {}
    if payload.get("type") in CODEX_BOOKKEEPING_TYPES and isinstance(info, dict):
        is_usage_entry = isinstance(info.get("last_token_usage"), dict)
    if not is_usage_entry:
        observe_codex_entry(entry, state)

    at = _timestamp(entry.get("timestamp"))
    if at is None:
        return None
    is_message = _is_codex_message_payload(payload)
    is_user = _is_codex_user_payload(payload)
    usage = {}
    if payload.get("type") in CODEX_BOOKKEEPING_TYPES and isinstance(info, dict):
        candidate = info.get("last_token_usage") or {}
        if isinstance(candidate, dict):
            usage = candidate
    input_total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    if not is_message and input_total + output <= 0:
        return None
    request_id = None
    if usage:
        identity = json.dumps(info, sort_keys=True, separators=(",", ":"))
        request_id = "codex-token:" + hashlib.sha256(identity.encode()).hexdigest()
    breakdown = (
        allocate_usage(
            state,
            input_tokens=max(input_total - cached, 0),
            output_tokens=output,
            cache_read_tokens=cached,
            reasoning_tokens=int(usage.get("reasoning_output_tokens") or 0),
        )
        if usage
        else {}
    )
    return _event(
        at=at,
        session_id=state["session_id"],
        cwd=state.get("cwd"),
        model=str((info.get("model") if isinstance(info, dict) else None) or state["current_model"]),
        surface=state["current_surface"],
        is_message=is_message,
        is_user=is_user,
        input_tokens=max(input_total - cached, 0),
        output_tokens=output,
        cache_read_tokens=cached,
        reasoning_tokens=int(usage.get("reasoning_output_tokens") or 0),
        request_id=request_id,
        usage_breakdown=breakdown,
        compaction_generation=int(state.get("compaction_generation") or 0),
    )


def _file_key(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}"


def _cursor_hash(path: Path, offset: int) -> str | None:
    try:
        with path.open("rb") as handle:
            start = max(offset - 256, 0)
            handle.seek(start)
            return hashlib.sha256(handle.read(offset - start)).hexdigest()
    except OSError:
        return None


def _provider_files(provider: str) -> Iterable[Path]:
    if provider == "claude":
        from src.scanners import claude_jsonl_files

        return (Path(path) for path in claude_jsonl_files())
    from src.codex_sources import codex_session_files

    return codex_session_files()


def _cursor(
    conn: sqlite3.Connection,
    provider: str,
    path: Path,
    stat: os.stat_result,
) -> sqlite3.Row | None:
    direct = conn.execute(
        "SELECT * FROM usage_file_cursors WHERE provider = ? AND path = ?",
        (provider, str(path)),
    ).fetchone()
    if direct is not None:
        return direct
    return conn.execute(
        """SELECT * FROM usage_file_cursors
           WHERE provider = ? AND device = ? AND inode = ?
           ORDER BY updated_at DESC LIMIT 1""",
        (provider, stat.st_dev, stat.st_ino),
    ).fetchone()


def _zero_duplicate_usage(event: dict, *, activity: bool = False) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "reasoning_tokens",
    ):
        event[key] = 0
    event["request_id"] = None
    event["usage_breakdown"] = {}
    if activity:
        event["is_message"] = 0
        event["is_user"] = 0


def _index_file(
    conn: sqlite3.Connection,
    provider: str,
    path: Path,
    parser: Callable[[dict, dict], dict | None],
) -> tuple[int, set[str], set[str]]:
    try:
        stat = path.stat()
    except OSError:
        return 0, set(), set()
    previous = _cursor(conn, provider, path, stat)
    reusable = bool(
        previous
        and previous["device"] == stat.st_dev
        and previous["inode"] == stat.st_ino
        and stat.st_size >= previous["offset_bytes"]
        and previous["cursor_hash"]
        and _cursor_hash(path, int(previous["offset_bytes"])) == previous["cursor_hash"]
    )
    file_key = str(previous["file_key"]) if reusable else _file_key(stat)
    offset = int(previous["offset_bytes"]) if reusable else 0
    if provider == "codex" and not reusable:
        from src.codex_sources import isolated_codex_default_model

        initial_model = isolated_codex_default_model(path) or "unknown"
    else:
        initial_model = "unknown"
    if provider == "claude" and not reusable:
        from src.claude_sources import claude_surface_for_path

        initial_surface = claude_surface_for_path(path)
    else:
        initial_surface = "unknown"
    state = {
        "current_model": str(previous["current_model"] or "unknown") if reusable else initial_model,
        "current_surface": str(previous["current_surface"] or "unknown") if reusable else initial_surface,
        "session_id": str(previous["session_id"] or path) if reusable else str(path),
        "cwd": str(previous["cwd"]) if reusable and previous["cwd"] else None,
    }
    from src.usage_explanation import (
        new_parser_state,
        persist_parser_state,
        restore_parser_state,
    )

    try:
        saved_parser_state = json.loads(previous["parser_state_json"]) if reusable else None
    except (TypeError, json.JSONDecodeError):
        saved_parser_state = None
    explanation_state = restore_parser_state(saved_parser_state) if reusable else new_parser_state()
    state.update(explanation_state)
    if not reusable:
        if provider == "claude":
            state["is_subagent"] = path.name.startswith("agent-") or "subagents" in path.parts
    if stat.st_size == offset:
        return 0, set(), set()

    inserted = 0
    affected_days: set[str] = set()
    affected_sessions: set[str] = set()
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
                try:
                    entry = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                event = parser(entry, state)
                if event is None:
                    continue
                request_id = event.get("request_id")
                if request_id:
                    request_file_key = (
                        str(event["session_id"])
                        if provider == "codex" and str(request_id).startswith("codex-token:")
                        else "global" if provider == "claude" else file_key
                    )
                    before = conn.total_changes
                    conn.execute(
                        """INSERT OR IGNORE INTO usage_request_ids(
                               provider, file_key, request_id
                           ) VALUES (?, ?, ?)""",
                        (provider, request_file_key, str(request_id)),
                    )
                    if conn.total_changes == before:
                        _zero_duplicate_usage(event, activity=provider == "claude")
                values = (
                    provider,
                    file_key,
                    line_offset,
                    str(path),
                    event["timestamp_us"],
                    event["day"],
                    event["session_id"],
                    event["cwd"],
                    event["model"],
                    event["surface"],
                    event["is_message"],
                    event["is_user"],
                    event["input_tokens"],
                    event["output_tokens"],
                    event["cache_read_tokens"],
                    event["cache_write_5m_tokens"],
                    event["cache_write_1h_tokens"],
                    event["reasoning_tokens"],
                    event["request_id"],
                    json.dumps(event["usage_breakdown"], separators=(",", ":")),
                    event["compaction_generation"],
                )
                before = conn.total_changes
                conn.execute(
                    """INSERT OR IGNORE INTO usage_events(
                           provider, file_key, line_offset, path, timestamp_us, day,
                           session_id, cwd, model, surface, is_message, is_user, input_tokens,
                           output_tokens, cache_read_tokens, cache_write_5m_tokens,
                           cache_write_1h_tokens, reasoning_tokens, request_id,
                           usage_breakdown_json, compaction_generation
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                if conn.total_changes > before:
                    inserted += 1
                    affected_days.add(str(event["day"]))
                    affected_sessions.add(str(event["session_id"]))
    except OSError:
        return 0, set(), set()

    conn.execute(
        """INSERT INTO usage_file_cursors(
               provider, path, file_key, device, inode, offset_bytes, size_bytes,
               current_model, current_surface, session_id, cwd, parser_state_json,
               cursor_hash, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, path) DO UPDATE SET
               file_key = excluded.file_key,
               device = excluded.device,
               inode = excluded.inode,
               offset_bytes = excluded.offset_bytes,
               size_bytes = excluded.size_bytes,
               current_model = excluded.current_model,
               current_surface = excluded.current_surface,
               session_id = excluded.session_id,
               cwd = excluded.cwd,
               parser_state_json = excluded.parser_state_json,
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
            state["current_model"],
            state["current_surface"],
            state["session_id"],
            state.get("cwd"),
            json.dumps(persist_parser_state(state), separators=(",", ":")),
            _cursor_hash(path, next_offset) or "",
            int(time.time()),
        ),
    )
    return inserted, affected_days, affected_sessions


def _rebuild_daily_models(
    conn: sqlite3.Connection,
    provider: str,
    days: set[str],
) -> None:
    updated_at = int(time.time())
    for day in days:
        conn.execute(
            "DELETE FROM usage_daily_models WHERE provider = ? AND day = ?",
            (provider, day),
        )
        conn.execute(
            """INSERT INTO usage_daily_models(
                   provider, day, model, messages, user_messages, sessions,
                   requests, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens,
                   reasoning_tokens, updated_at
               )
               SELECT
                   provider, day, model,
                   SUM(is_message), SUM(is_user), COUNT(DISTINCT session_id),
                   SUM(CASE WHEN input_tokens + output_tokens + cache_read_tokens
                                + cache_write_5m_tokens + cache_write_1h_tokens > 0
                            THEN 1 ELSE 0 END),
                   SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
                   SUM(cache_write_5m_tokens), SUM(cache_write_1h_tokens),
                   SUM(reasoning_tokens), ?
               FROM usage_events
               WHERE provider = ? AND day = ?
               GROUP BY provider, day, model""",
            (updated_at, provider, day),
        )


def sync_provider(provider: str) -> dict:
    """Consume complete lines appended since the previous provider sync."""
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider: {provider}")
    init()
    parser = _parse_claude_entry if provider == "claude" else _parse_codex_entry
    inserted = 0
    affected_days: set[str] = set()
    affected_sessions: set[str] = set()
    files = 0
    with closing(_connect()) as conn:
        for path in _provider_files(provider):
            files += 1
            count, days, sessions = _index_file(conn, provider, path, parser)
            inserted += count
            affected_days.update(days)
            affected_sessions.update(sessions)
        _rebuild_daily_models(conn, provider, affected_days)
        conn.commit()
    if affected_sessions:
        from src.work_session_sync import sync_normalized_sessions

        sync_normalized_sessions(provider, affected_sessions)
    return {
        "provider": provider,
        "files": files,
        "events_inserted": inserted,
        "affected_sessions": sorted(affected_sessions),
    }


def iter_events_between(provider: str, start: datetime, end: datetime) -> Iterable[dict]:
    """Stream normalized provider events in an exact inclusive time window."""
    init()
    start_us = round(start.astimezone(timezone.utc).timestamp() * 1_000_000)
    end_us = round(end.astimezone(timezone.utc).timestamp() * 1_000_000)
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT timestamp_us, session_id, cwd, model, surface, is_message, is_user,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_write_5m_tokens, cache_write_1h_tokens,
                      reasoning_tokens, usage_breakdown_json, compaction_generation
               FROM usage_events
               WHERE provider = ? AND timestamp_us BETWEEN ? AND ?
               ORDER BY timestamp_us, file_key, line_offset""",
            (provider, start_us, end_us),
        )
        for row in rows:
            yield {
                **dict(row),
                "usage_breakdown": json.loads(row["usage_breakdown_json"] or "{}"),
                "at": datetime.fromtimestamp(
                    row["timestamp_us"] / 1_000_000,
                    tz=timezone.utc,
                ),
            }
    finally:
        conn.close()


def events_between(provider: str, start: datetime, end: datetime) -> list[dict]:
    """Materialize exact-window events for diagnostics and tests."""
    return list(iter_events_between(provider, start, end))


def daily_models(provider: str, start_day: str, end_day: str) -> list[dict]:
    """Read persisted daily/model aggregates for reporting and diagnostics."""
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT * FROM usage_daily_models
               WHERE provider = ? AND day BETWEEN ? AND ?
               ORDER BY day, model""",
            (provider, start_day, end_day),
        ).fetchall()
    return [dict(row) for row in rows]


def providers_for_day(day: str) -> list[str]:
    """Which providers recorded usage on ONE day.

    The cheap half of periodic emission. A 60s job that asked "which provider-days
    exist" without the day bound would read the whole history every minute to emit a
    single day; this reads one day's slice and nothing else.
    """
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT provider FROM usage_daily_models
               WHERE day = ?
               ORDER BY provider""",
            (day,),
        ).fetchall()
    return [row["provider"] for row in rows]


def provider_days() -> list[tuple[str, str]]:
    """Every (provider, day) in the ledger, oldest first.

    Deliberately unbounded, and deliberately NOT what the periodic cycle calls. This is
    the backfill's input: a fresh install, or one that upgraded before emission was
    wired up, has to walk all of history exactly once. `providers_for_day` is the
    bounded query the recurring path uses.
    """
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT provider, day FROM usage_daily_models
               ORDER BY day, provider"""
        ).fetchall()
    return [(row["provider"], row["day"]) for row in rows]
