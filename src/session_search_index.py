"""Persistent, contentless exact-search index for local session evidence."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from src import session_evidence, work_ledger


_SCHEMA_VERSION = 2
_MAX_SESSIONS = 2_000
_SOURCE_CHECKPOINT_BYTES = 4 * 1024
_initialized_paths: set[str] = set()
_init_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refreshing_paths: set[str] = set()


class _SourceChanged(RuntimeError):
    pass


def _db_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_SESSION_SEARCH_DB")
    return Path(configured).expanduser() if configured else Path.home() / ".usage-tracker" / "session-search.db"


def _connect_raw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    path = _db_path()
    key = str(path)
    if key in _initialized_paths:
        return
    with _init_lock:
        if key in _initialized_paths:
            return
        with closing(_connect_raw(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS search_sources(
                    session_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_session_id TEXT NOT NULL,
                    source_path TEXT,
                    mtime_ns INTEGER,
                    size_bytes INTEGER,
                    checkpoint_start INTEGER,
                    checkpoint_hash TEXT,
                    metadata_hash TEXT NOT NULL,
                    document_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    indexed_at_us INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_search_sources_provider
                ON search_sources(provider);

                CREATE TABLE IF NOT EXISTS search_documents(
                    rowid INTEGER PRIMARY KEY,
                    document_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES search_sources(session_id)
                        ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    evidence_id TEXT,
                    occurred_at_us INTEGER,
                    kind TEXT NOT NULL,
                    source_line_offset INTEGER,
                    source_item_index INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_search_documents_session
                ON search_documents(session_id);
                """
            )
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                           title, summary, tool_name, file_paths,
                           content='', contentless_delete=1,
                           tokenize='unicode61'
                       )"""
                )
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    "Session search requires SQLite FTS5 with contentless-delete support"
                ) from exc
            source_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(search_sources)")
            }
            if "checkpoint_start" not in source_columns:
                conn.execute("ALTER TABLE search_sources ADD COLUMN checkpoint_start INTEGER")
            if "checkpoint_hash" not in source_columns:
                conn.execute("ALTER TABLE search_sources ADD COLUMN checkpoint_hash TEXT")
            conn.execute(
                """INSERT INTO search_meta(key, value) VALUES ('schema_version', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(_SCHEMA_VERSION),),
            )
            conn.commit()
        _initialized_paths.add(key)


def _connect() -> sqlite3.Connection:
    init()
    return _connect_raw(_db_path())


def _metadata_values(session: dict) -> list[str]:
    return [
        session_evidence._display_name(session),
        str(session.get("repository_name") or ""),
        str(session.get("branch") or ""),
        str(session.get("cwd") or ""),
        " ".join(str(model) for model in session.get("models") or []),
    ]


def _metadata_hash(session: dict) -> str:
    encoded = json.dumps(
        _metadata_values(session),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_stat(session: dict) -> tuple[Path | None, os.stat_result | None]:
    value = str(session.get("source_path") or "").strip()
    if not value:
        return None, None
    path = Path(value)
    try:
        return path, path.stat()
    except OSError:
        return path, None


def _source_checkpoint(path: Path, size: int) -> tuple[int, str]:
    start = max(0, size - _SOURCE_CHECKPOINT_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        content = handle.read(size - start)
    return start, hashlib.sha256(content).hexdigest()


def _delete_session_documents(conn: sqlite3.Connection, session_id: str) -> None:
    rows = conn.execute(
        "SELECT rowid FROM search_documents WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    conn.executemany(
        "DELETE FROM search_fts WHERE rowid = ?",
        ((row["rowid"],) for row in rows),
    )
    conn.execute("DELETE FROM search_documents WHERE session_id = ?", (session_id,))


def _insert_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    session_id: str,
    provider: str,
    evidence_id: str | None,
    occurred_at_us: int | None,
    kind: str,
    source_line_offset: int | None,
    source_item_index: int | None,
    title: str,
    summary: str,
    tool_name: str = "",
    file_paths: str = "",
) -> None:
    cursor = conn.execute(
        """INSERT INTO search_documents(
               document_id, session_id, provider, evidence_id, occurred_at_us,
               kind, source_line_offset, source_item_index
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            document_id,
            session_id,
            provider,
            evidence_id,
            occurred_at_us,
            kind,
            source_line_offset,
            source_item_index,
        ),
    )
    conn.execute(
        """INSERT INTO search_fts(rowid, title, summary, tool_name, file_paths)
           VALUES (?, ?, ?, ?, ?)""",
        (cursor.lastrowid, title, summary, tool_name, file_paths),
    )


def _replace_session(conn: sqlite3.Connection, session: dict) -> tuple[str, int]:
    session_id = str(session["id"])
    provider = str(session["provider"])
    provider_session_id = str(session["provider_session_id"])
    path, before = _source_stat(session)
    checkpoint = _source_checkpoint(path, before.st_size) if path and before else None
    metadata_hash = _metadata_hash(session)
    indexed_at_us = time.time_ns() // 1_000
    document_count = 0
    status = "ready" if before is not None else "missing"

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO search_sources(
                   session_id, provider, provider_session_id, source_path,
                   mtime_ns, size_bytes, checkpoint_start, checkpoint_hash,
                   metadata_hash, document_count,
                   status, last_error, indexed_at_us
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   provider = excluded.provider,
                   provider_session_id = excluded.provider_session_id,
                   source_path = excluded.source_path,
                   mtime_ns = excluded.mtime_ns,
                   size_bytes = excluded.size_bytes,
                   checkpoint_start = excluded.checkpoint_start,
                   checkpoint_hash = excluded.checkpoint_hash,
                   metadata_hash = excluded.metadata_hash,
                   document_count = 0,
                   status = excluded.status,
                   last_error = NULL,
                   indexed_at_us = excluded.indexed_at_us""",
            (
                session_id,
                provider,
                provider_session_id,
                str(path) if path else None,
                before.st_mtime_ns if before else None,
                before.st_size if before else None,
                checkpoint[0] if checkpoint else None,
                checkpoint[1] if checkpoint else None,
                metadata_hash,
                status,
                indexed_at_us,
            ),
        )
        _delete_session_documents(conn, session_id)

        metadata = _metadata_values(session)
        _insert_document(
            conn,
            document_id=work_ledger.stable_id("search_session", session_id),
            session_id=session_id,
            provider=provider,
            evidence_id=None,
            occurred_at_us=session.get("source_modified_at_us")
            or session.get("ended_at_us")
            or session.get("started_at_us"),
            kind="session",
            source_line_offset=None,
            source_item_index=None,
            title="Session",
            summary=" ".join(metadata),
        )
        document_count += 1

        if path is not None and before is not None:
            with path.open("rb") as handle:
                while True:
                    line_offset = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    try:
                        entry = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    for item in session_evidence.extract_entry(
                        provider,
                        provider_session_id,
                        entry,
                        line_offset=line_offset,
                        include_source=True,
                    ):
                        _insert_document(
                            conn,
                            document_id=str(item["id"]),
                            session_id=session_id,
                            provider=provider,
                            evidence_id=str(item["id"]),
                            occurred_at_us=item.get("occurred_at_us"),
                            kind=str(item["kind"]),
                            source_line_offset=int(item["source_line_offset"]),
                            source_item_index=int(item["source_item_index"]),
                            title=str(item["title"]),
                            summary=str(item["summary"]),
                            tool_name=str(item.get("tool_name") or ""),
                            file_paths=" ".join(item.get("file_paths") or []),
                        )
                        document_count += 1
            try:
                after = path.stat()
            except OSError as exc:
                raise _SourceChanged("Session source disappeared during indexing") from exc
            if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
                raise _SourceChanged("Session source changed during indexing")

        conn.execute(
            """UPDATE search_sources
               SET document_count = ?, status = ?, indexed_at_us = ?
               WHERE session_id = ?""",
            (document_count, status, indexed_at_us, session_id),
        )
        conn.commit()
        return status, document_count
    except Exception:
        conn.rollback()
        raise


def _mark_stale(conn: sqlite3.Connection, session: dict, error: str) -> None:
    conn.execute(
        """INSERT INTO search_sources(
               session_id, provider, provider_session_id, source_path,
               mtime_ns, size_bytes, metadata_hash, document_count,
               status, last_error, indexed_at_us
           ) VALUES (?, ?, ?, ?, NULL, NULL, ?, 0, 'stale', ?, NULL)
           ON CONFLICT(session_id) DO UPDATE SET
               status = 'stale', last_error = excluded.last_error""",
        (
            session["id"],
            session["provider"],
            session["provider_session_id"],
            session.get("source_path"),
            _metadata_hash(session),
            error,
        ),
    )
    conn.commit()


def _source_is_current(session: dict, row: sqlite3.Row | None) -> bool:
    if row is None or row["metadata_hash"] != _metadata_hash(session):
        return False
    path, current = _source_stat(session)
    if (str(path) if path else None) != row["source_path"]:
        return False
    if current is None:
        return row["status"] == "missing"
    return (
        row["status"] == "ready"
        and row["mtime_ns"] == current.st_mtime_ns
        and row["size_bytes"] == current.st_size
        and row["checkpoint_start"] is not None
        and bool(row["checkpoint_hash"])
    )


def refresh(*, provider: str | None = None) -> dict:
    if provider is not None and provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    path_key = str(_db_path())
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "busy", **status(provider=provider)}
    _refreshing_paths.add(path_key)
    try:
        sessions = work_ledger.list_ai_sessions(provider=provider, limit=_MAX_SESSIONS)
        session_ids = {str(session["id"]) for session in sessions}
        changed = skipped = missing = stale = documents = 0
        errors: list[str] = []
        with closing(_connect()) as conn:
            rows = {
                row["session_id"]: row
                for row in conn.execute(
                    "SELECT * FROM search_sources" + (" WHERE provider = ?" if provider else ""),
                    (provider,) if provider else (),
                ).fetchall()
            }
            for session in sessions:
                row = rows.get(session["id"])
                if _source_is_current(session, row):
                    skipped += 1
                    missing += int(row["status"] == "missing")
                    documents += int(row["document_count"])
                    continue
                try:
                    source_status, count = _replace_session(conn, session)
                    changed += 1
                    missing += int(source_status == "missing")
                    documents += count
                except _SourceChanged as exc:
                    stale += 1
                    errors.append(f"{session['id']}: {exc}")
                    _mark_stale(conn, session, str(exc))
                except (OSError, sqlite3.Error, ValueError) as exc:
                    stale += 1
                    errors.append(f"{session['id']}: {exc}")
                    _mark_stale(conn, session, str(exc))

            removed = 0
            if len(sessions) < _MAX_SESSIONS:
                for stale_id in set(rows) - session_ids:
                    conn.execute("BEGIN IMMEDIATE")
                    _delete_session_documents(conn, stale_id)
                    conn.execute("DELETE FROM search_sources WHERE session_id = ?", (stale_id,))
                    conn.commit()
                    removed += 1
            conn.execute(
                """INSERT INTO search_meta(key, value) VALUES ('last_error', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                ("; ".join(errors[:5]),),
            )
            conn.commit()
        result = status(provider=provider)
        result.update({
            "status": "partial" if errors else "ok",
            "sessions_changed": changed,
            "sessions_skipped": skipped,
            "sessions_removed": removed,
            "documents_processed": documents,
            "errors": errors[:5],
        })
        return result
    finally:
        _refreshing_paths.discard(path_key)
        _refresh_lock.release()


def _fts_query(query: str) -> str | None:
    terms = re.findall(r"\w+", query.casefold(), flags=re.UNICODE)
    if not terms:
        return None
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)


def _materialize(
    row: sqlite3.Row,
    session: dict,
    query_terms: list[str],
) -> dict | None:
    session_name = session_evidence._display_name(session)
    if row["kind"] == "session":
        item = {
            "id": None,
            "occurred_at_us": row["occurred_at_us"],
            "kind": "session",
            "title": "Session",
            "summary": session_name,
        }
        match_values: list[object] = _metadata_values(session)
    else:
        source = session.get("source_path")
        if not source or row["source_line_offset"] is None:
            return None
        try:
            with Path(source).open("rb") as handle:
                handle.seek(int(row["source_line_offset"]))
                entry = json.loads(handle.readline().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(entry, dict):
            return None
        items = session_evidence.extract_entry(
            str(session["provider"]),
            str(session["provider_session_id"]),
            entry,
            line_offset=int(row["source_line_offset"]),
        )
        item = next((value for value in items if value["id"] == row["evidence_id"]), None)
        if item is None:
            return None
        match_values = [
            item["title"],
            item["summary"],
            item.get("tool_name"),
            " ".join(item.get("file_paths") or []),
        ]
    if not session_evidence._matches_query(
        query_terms,
        *match_values,
    ):
        return None
    return {
        "session_id": session["id"],
        "provider": session["provider"],
        "session_name": session_name,
        "repository_name": session.get("repository_name"),
        "branch": session.get("branch"),
        "evidence_id": item.get("id"),
        "occurred_at_us": item.get("occurred_at_us"),
        "kind": item["kind"],
        "title": item["title"],
        "snippet": item["summary"],
    }


def _append_tail_is_covered(session: dict, row: sqlite3.Row | None) -> bool:
    if row is None or row["status"] != "ready":
        return False
    if row["metadata_hash"] != _metadata_hash(session):
        return False
    path, current = _source_stat(session)
    if current is None or (str(path) if path else None) != row["source_path"]:
        return False
    indexed_size = int(row["size_bytes"] or 0)
    if not indexed_size <= current.st_size <= indexed_size + session_evidence._SEARCH_BYTES:
        return False
    checkpoint_start = row["checkpoint_start"]
    checkpoint_hash = row["checkpoint_hash"]
    if checkpoint_start is None or not checkpoint_hash:
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(int(checkpoint_start))
            content = handle.read(indexed_size - int(checkpoint_start))
    except OSError:
        return False
    return hashlib.sha256(content).hexdigest() == checkpoint_hash


def search(
    query: str,
    *,
    provider: str | None = None,
    limit: int = 50,
    session_limit: int = 500,
) -> dict:
    normalized_query = session_evidence._safe_text(query, limit=200)
    query_terms = normalized_query.casefold().split()
    if len(normalized_query) < 2 or not query_terms:
        raise ValueError("query must contain at least 2 characters")
    if provider is not None and provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    bounded_limit = max(1, min(int(limit), 100))
    sessions = work_ledger.list_ai_sessions(provider=provider, limit=_MAX_SESSIONS)
    index_status = status(provider=provider, sessions=sessions)
    if index_status["sessions_indexed"] == 0:
        result = session_evidence.bounded_search(
            normalized_query,
            provider=provider,
            limit=bounded_limit,
            session_limit=session_limit,
            sessions=sessions,
        )
        result["sessions_available"] = len(sessions)
        result["index"] = index_status
        return result

    session_map = {session["id"]: session for session in sessions}
    with closing(_connect()) as conn:
        source_rows = {
            row["session_id"]: row
            for row in conn.execute(
                "SELECT * FROM search_sources" + (" WHERE provider = ?" if provider else ""),
                (provider,) if provider else (),
            ).fetchall()
        }
        indexed_ids = set(session_map) & set(source_rows)
        fts_query = _fts_query(normalized_query)
        indexed_matches: list[dict] = []
        if fts_query and indexed_ids:
            clauses = ["search_fts MATCH ?"]
            params: list[object] = [fts_query]
            if provider:
                clauses.append("documents.provider = ?")
                params.append(provider)
            placeholders = ",".join("?" for _ in indexed_ids)
            clauses.append(f"documents.session_id IN ({placeholders})")
            params.extend(sorted(indexed_ids))
            params.append(bounded_limit * 10)
            rows = conn.execute(
                f"""SELECT documents.*, bm25(search_fts) AS rank
                    FROM search_fts
                    JOIN search_documents AS documents
                      ON documents.rowid = search_fts.rowid
                    WHERE {' AND '.join(clauses)}
                    ORDER BY rank, COALESCE(documents.occurred_at_us, 0) DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            for row in rows:
                session_id = row["session_id"]
                match = _materialize(row, session_map[session_id], query_terms)
                if match is not None:
                    indexed_matches.append(match)
                if len(indexed_matches) >= bounded_limit:
                    break

    # Recheck after materialization so an actively written source cannot leave
    # a request pinned to an obsolete index generation.
    stale_sessions = [
        session for session_id, session in session_map.items()
        if not _source_is_current(session, source_rows.get(session_id))
    ]
    fallback = session_evidence.bounded_search(
        normalized_query,
        provider=provider,
        limit=bounded_limit,
        session_limit=session_limit,
        sessions=stale_sessions,
    )
    live_tail_covered = all(
        _append_tail_is_covered(session, source_rows.get(session["id"]))
        for session in stale_sessions
    )
    matches = indexed_matches + fallback["matches"]
    deduplicated: dict[tuple, dict] = {}
    for match in matches:
        key = (match["session_id"], match.get("evidence_id"), match["kind"])
        deduplicated[key] = match
    ordered = sorted(
        deduplicated.values(),
        key=lambda item: int(item.get("occurred_at_us") or 0),
        reverse=True,
    )
    return {
        "query": normalized_query,
        "matches": ordered[:bounded_limit],
        "sessions_scanned": fallback["sessions_scanned"],
        "sessions_available": len(sessions),
        "bytes_scanned": fallback["bytes_scanned"],
        "missing_sources": index_status["missing_sources"],
        "partial": (
            len(sessions) >= _MAX_SESSIONS
            or fallback["missing_sources"] > 0
            or (fallback["partial"] and not live_tail_covered)
        ),
        "index": index_status,
    }


def status(*, provider: str | None = None, sessions: list[dict] | None = None) -> dict:
    if provider is not None and provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    available = sessions if sessions is not None else work_ledger.list_ai_sessions(
        provider=provider,
        limit=_MAX_SESSIONS,
    )
    path = _db_path()
    with closing(_connect()) as conn:
        where = " WHERE provider = ?" if provider else ""
        params = (provider,) if provider else ()
        source = conn.execute(
            f"""SELECT COUNT(*) AS sessions_indexed,
                       COALESCE(SUM(document_count), 0) AS documents,
                       SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale_sessions,
                       SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_sources,
                       MAX(indexed_at_us) AS last_indexed_at_us
                FROM search_sources{where}""",
            params,
        ).fetchone()
        error_row = conn.execute(
            "SELECT value FROM search_meta WHERE key = 'last_error'"
        ).fetchone()
        rows = {
            row["session_id"]: row
            for row in conn.execute(
                f"SELECT * FROM search_sources{where}",
                params,
            ).fetchall()
        }
    indexed = int(source["sessions_indexed"] or 0)
    last_error = str(error_row["value"] or "") if error_row else ""
    stale_sessions = sum(
        1 for session in available
        if not _source_is_current(session, rows.get(session["id"]))
    )
    state = "updating" if str(path) in _refreshing_paths else "ready" if indexed else "empty"
    if last_error and not indexed:
        state = "error"
    return {
        "state": state,
        "backend": "sqlite_fts5",
        "privacy_mode": "contentless",
        "sessions_indexed": indexed,
        "sessions_available": len(available),
        "documents": int(source["documents"] or 0),
        "stale_sessions": max(stale_sessions, int(source["stale_sessions"] or 0)),
        "missing_sources": int(source["missing_sources"] or 0),
        "last_indexed_at_us": source["last_indexed_at_us"],
        "db_bytes": path.stat().st_size if path.exists() else 0,
        "last_error": last_error or None,
    }
