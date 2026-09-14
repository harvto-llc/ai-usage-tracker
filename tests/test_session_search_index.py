import json
import os
import sqlite3

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

from src import session_evidence, session_search_index, usage_ledger, work_ledger
from src.api import API_SECRET, app


AUTH = {"Authorization": f"Bearer {API_SECRET}"}
AT = "2026-07-25T12:00:00Z"


@pytest.fixture(autouse=True)
def databases(tmp_path, monkeypatch):
    activity_path = tmp_path / "activity.db"
    search_path = tmp_path / "session-search.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(activity_path))
    monkeypatch.setenv("USAGE_TRACKER_SESSION_SEARCH_DB", str(search_path))
    usage_ledger._initialized_paths.discard(str(activity_path))
    work_ledger._initialized_paths.discard(str(activity_path))
    session_search_index._initialized_paths.discard(str(search_path))
    session_evidence._cache.clear()


def _append_jsonl(path, entries):
    with path.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _codex_message(text, *, role="assistant"):
    return {
        "timestamp": AT,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _session(provider, provider_id, path, *, title=None):
    return work_ledger.upsert_ai_session({
        "provider": provider,
        "provider_session_id": provider_id,
        "source_path": str(path),
        "native_title": title or f"{provider.title()} indexed evidence",
        "source_modified_at_us": 1_753_444_800_000_000,
    })


def test_init_migrates_pre_checkpoint_source_schema():
    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    conn.execute(
        """CREATE TABLE search_sources(
               session_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
               provider_session_id TEXT NOT NULL, source_path TEXT,
               mtime_ns INTEGER, size_bytes INTEGER, metadata_hash TEXT NOT NULL,
               document_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
               last_error TEXT, indexed_at_us INTEGER
           )"""
    )
    conn.commit()
    conn.close()

    session_search_index.init()

    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    columns = {row[1] for row in conn.execute("PRAGMA table_info(search_sources)")}
    conn.close()
    assert {"checkpoint_start", "checkpoint_hash"} <= columns


def test_contentless_index_materializes_source_without_storing_document_text(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("Reconciled lunar billing credits")])
    session_id = _session("codex", "codex-index", source)

    refresh = session_search_index.refresh()
    result = session_evidence.search("lunar billing")

    assert refresh["status"] == "ok"
    assert result["matches"][0]["session_id"] == session_id
    assert result["matches"][0]["snippet"] == "Reconciled lunar billing credits"
    assert result["sessions_scanned"] == 0
    assert result["index"]["privacy_mode"] == "contentless"

    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    columns = {row[1] for row in conn.execute("PRAGMA table_info(search_documents)")}
    fts_values = conn.execute(
        "SELECT title, summary, tool_name, file_paths FROM search_fts"
    ).fetchall()
    conn.close()
    assert "summary" not in columns
    assert "body" not in columns
    assert fts_values and all(value is None for row in fts_values for value in row)


def test_refresh_is_incremental_and_replaces_changed_session_without_duplicates(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("First indexed phrase")])
    _session("codex", "codex-incremental", source)

    first = session_search_index.refresh()
    unchanged = session_search_index.refresh()
    _append_jsonl(source, [_codex_message("Second indexed phrase")])
    changed = session_search_index.refresh()

    assert first["sessions_changed"] == 1
    assert unchanged["sessions_changed"] == 0
    assert unchanged["sessions_skipped"] == 1
    assert changed["sessions_changed"] == 1
    assert session_evidence.search("Second indexed")["matches"][0]["snippet"] == "Second indexed phrase"

    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    document_ids = [row[0] for row in conn.execute("SELECT document_id FROM search_documents")]
    conn.close()
    assert len(document_ids) == len(set(document_ids)) == 3


def test_refresh_backfills_source_checkpoint_after_schema_migration(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("Checkpoint migration phrase")])
    _session("codex", "codex-checkpoint", source)
    session_search_index.refresh()
    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    conn.execute(
        "UPDATE search_sources SET checkpoint_start = NULL, checkpoint_hash = NULL"
    )
    conn.commit()
    conn.close()

    refresh = session_search_index.refresh()

    assert refresh["sessions_changed"] == 1
    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    checkpoint = conn.execute(
        "SELECT checkpoint_start, checkpoint_hash FROM search_sources"
    ).fetchone()
    conn.close()
    assert checkpoint[0] is not None
    assert checkpoint[1]


def test_stale_session_uses_bounded_fallback_until_next_refresh(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("Old indexed content")])
    session_id = _session("codex", "codex-live", source)
    session_search_index.refresh()

    _append_jsonl(source, [_codex_message("Fresh unindexed comet phrase")])
    result = session_evidence.search("comet phrase")

    assert result["matches"][0]["session_id"] == session_id
    assert result["matches"][0]["snippet"] == "Fresh unindexed comet phrase"
    assert result["sessions_scanned"] == 1
    assert result["index"]["stale_sessions"] == 1
    assert result["partial"] is False

    old_result = session_evidence.search("Old indexed")
    assert old_result["matches"][0]["snippet"] == "Old indexed content"
    assert old_result["partial"] is False


def test_rewritten_source_does_not_return_false_index_match(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("Original aurora phrase")])
    _session("codex", "codex-rewritten", source)
    session_search_index.refresh()

    source.write_text(json.dumps(_codex_message("Replacement ocean phrase")) + "\n")
    old_result = session_evidence.search("aurora phrase")
    new_result = session_evidence.search("ocean phrase")

    assert old_result["matches"] == []
    assert old_result["partial"] is False
    assert new_result["matches"][0]["snippet"] == "Replacement ocean phrase"


def test_provider_filter_and_metadata_search_use_index(tmp_path):
    codex_source = tmp_path / "codex.jsonl"
    claude_source = tmp_path / "claude.jsonl"
    _append_jsonl(codex_source, [_codex_message("Shared nebula phrase")])
    _append_jsonl(claude_source, [{
        "timestamp": AT,
        "message": {"role": "assistant", "content": "Shared nebula phrase"},
    }])
    codex_id = _session("codex", "codex-provider", codex_source, title="Orbit accounting")
    _session("claude", "claude-provider", claude_source)
    session_search_index.refresh()

    content = session_evidence.search("nebula", provider="codex")
    metadata = session_evidence.search("Orbit accounting", provider="codex")

    assert {match["provider"] for match in content["matches"]} == {"codex"}
    assert metadata["matches"][0]["session_id"] == codex_id
    assert metadata["matches"][0]["kind"] == "session"


def test_refresh_removes_archived_session_documents(tmp_path):
    source = tmp_path / "codex.jsonl"
    _append_jsonl(source, [_codex_message("Archived lunar phrase")])
    session_id = _session("codex", "codex-archived", source)
    session_search_index.refresh()

    conn = sqlite3.connect(os.environ["USAGE_TRACKER_ACTIVITY_DB"])
    conn.execute("UPDATE ai_sessions SET archived_at = 1 WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    refresh = session_search_index.refresh()

    assert refresh["sessions_removed"] == 1
    assert refresh["sessions_indexed"] == 0
    conn = sqlite3.connect(os.environ["USAGE_TRACKER_SESSION_SEARCH_DB"])
    assert conn.execute("SELECT COUNT(*) FROM search_documents").fetchone()[0] == 0
    conn.close()


def test_index_status_api_requires_auth_and_validates_provider():
    client = TestClient(app)

    assert client.get("/work-ledger/session-search/status").status_code == 401
    status = client.get("/work-ledger/session-search/status", headers=AUTH)
    invalid = client.get(
        "/work-ledger/session-search/status?provider=cursor",
        headers=AUTH,
    )

    assert status.status_code == 200
    assert status.json()["backend"] == "sqlite_fts5"
    assert status.json()["state"] == "empty"
    assert invalid.status_code == 400
