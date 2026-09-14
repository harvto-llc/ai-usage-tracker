import json
import os

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

from src import session_evidence, session_search_index, usage_ledger, work_ledger
from src.api import API_SECRET, app


AUTH = {"Authorization": f"Bearer {API_SECRET}"}
AT = "2026-07-25T12:00:00Z"


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    search_db_path = tmp_path / "session-search.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    monkeypatch.setenv("USAGE_TRACKER_SESSION_SEARCH_DB", str(search_db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    session_search_index._initialized_paths.discard(str(search_db_path))
    session_evidence._cache.clear()


def _write_jsonl(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def _session(provider, provider_id, path):
    return work_ledger.upsert_ai_session({
        "provider": provider,
        "provider_session_id": provider_id,
        "source_path": str(path),
        "native_title": f"{provider.title()} evidence",
        "source_modified_at_us": 1_753_444_800_000_000,
    })


def test_claude_timeline_extracts_messages_and_actionable_tools(tmp_path):
    source = tmp_path / "claude.jsonl"
    _write_jsonl(source, [
        {
            "timestamp": AT,
            "type": "user",
            "sessionId": "claude-1",
            "message": {
                "role": "user",
                "content": "Fix the billing report <system-reminder>hidden</system-reminder>",
            },
        },
        {
            "timestamp": AT,
            "type": "assistant",
            "sessionId": "claude-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I found the failing calculation."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "pytest tests/test_billing.py -q"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tool-2",
                        "name": "Edit",
                        "input": {"file_path": "/repo/src/billing.py", "new_string": "secret"},
                    },
                ],
            },
        },
        {
            "timestamp": AT,
            "type": "user",
            "sessionId": "claude-1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "private command output"}],
            },
        },
    ])
    session_id = _session("claude", "claude-1", source)

    timeline = session_evidence.session_timeline(session_id)

    assert [item["kind"] for item in timeline["items"]] == [
        "prompt", "response", "test", "edit",
    ]
    assert timeline["items"][0]["summary"] == "Fix the billing report"
    assert timeline["items"][2]["title"] == "Ran tests"
    assert timeline["items"][3]["file_paths"] == ["/repo/src/billing.py"]
    assert "private command output" not in json.dumps(timeline)


def test_codex_timeline_skips_developer_and_tool_outputs(tmp_path):
    source = tmp_path / "codex.jsonl"
    _write_jsonl(source, [
        {
            "timestamp": AT,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "bootstrap"}],
            },
        },
        {
            "timestamp": AT,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Find quota drift"}],
            },
        },
        {
            "timestamp": AT,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The weekly source is stale."}],
            },
        },
        {
            "timestamp": AT,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "git status --short"}),
            },
        },
        {
            "timestamp": AT,
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "private output"},
        },
    ])
    session_id = _session("codex", "codex-1", source)

    timeline = session_evidence.session_timeline(session_id)

    assert [item["kind"] for item in timeline["items"]] == [
        "prompt", "response", "git",
    ]
    assert timeline["items"][2]["summary"] == "git status --short"
    assert "bootstrap" not in json.dumps(timeline)
    assert "private output" not in json.dumps(timeline)


def test_first_prompt_preview_skips_internal_user_wrappers(tmp_path):
    source = tmp_path / "claude.jsonl"
    _write_jsonl(source, [
        {
            "timestamp": AT,
            "type": "user",
            "sessionId": "claude-preview",
            "message": {
                "role": "user",
                "content": (
                    "<command-message>model</command-message>"
                    "<command-name>/model</command-name>"
                    "<command-args></command-args>"
                ),
            },
        },
        {
            "timestamp": AT,
            "type": "user",
            "sessionId": "claude-preview",
            "message": {
                "role": "user",
                "content": "[Request interrupted by user]",
            },
        },
        {
            "timestamp": AT,
            "type": "user",
            "sessionId": "claude-preview",
            "message": {
                "role": "user",
                "content": (
                    "<realtime_delegation><input>"
                    "Assign this session to the Harvto project"
                    "</input><transcript_delta>transport transcript</transcript_delta>"
                    "</realtime_delegation>"
                ),
            },
        },
    ])
    session_id = _session("claude", "claude-preview", source)
    session = work_ledger.get_ai_session_by_id(session_id)

    assert session_evidence.first_prompt_preview(session) == (
        "Assign this session to the Harvto project"
    )


def test_tool_summaries_keep_mcp_action_private_and_classify_web_fetch():
    mcp = session_evidence._tool_evidence(
        "mcp__private-server__send_message",
        {"message": "private body", "target": "worker"},
    )
    web = session_evidence._tool_evidence(
        "WebFetch",
        {"url": "https://example.com/docs", "prompt": "private prompt"},
    )

    assert mcp["tool_name"] == "MCP"
    assert mcp["summary"] == "send message"
    assert "private-server" not in json.dumps(mcp)
    assert "private body" not in json.dumps(mcp)
    assert web["kind"] == "web"
    assert web["summary"] == "https://example.com/docs"


def test_search_matches_content_and_session_metadata(tmp_path):
    source = tmp_path / "codex.jsonl"
    _write_jsonl(source, [{
        "timestamp": AT,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Reconciled credit usage"}],
        },
    }])
    session_id = _session("codex", "codex-search", source)

    content = session_evidence.search("credit usage")
    metadata = session_evidence.search("Codex evidence")

    assert content["matches"][0]["session_id"] == session_id
    assert content["matches"][0]["snippet"] == "Reconciled credit usage"
    assert metadata["matches"][0]["kind"] == "session"
    assert content["sessions_scanned"] == 1
    assert content["partial"] is False


def test_search_session_name_prefers_provider_rename():
    assert session_evidence._display_name({
        "provider": "codex",
        "provider_session_id": "codex-renamed",
        "native_title": "Provider rename",
        "nickname": "Local alias",
    }) == "Provider rename"


def test_timeline_limit_keeps_most_recent_evidence(tmp_path):
    source = tmp_path / "claude.jsonl"
    _write_jsonl(source, [
        {
            "timestamp": f"2026-07-25T12:00:0{index}Z",
            "type": "user",
            "message": {"role": "user", "content": f"Prompt {index}"},
        }
        for index in range(3)
    ])
    session_id = _session("claude", "claude-tail", source)

    timeline = session_evidence.session_timeline(session_id, limit=2)

    assert [item["summary"] for item in timeline["items"]] == ["Prompt 1", "Prompt 2"]
    assert timeline["truncated"] is True


def test_search_stops_at_global_byte_budget(tmp_path, monkeypatch):
    for index in range(2):
        source = tmp_path / f"claude-{index}.jsonl"
        _write_jsonl(source, [{
            "timestamp": f"2026-07-25T12:00:0{index}Z",
            "type": "user",
            "message": {"role": "user", "content": "No matching phrase"},
        }])
        _session("claude", f"claude-budget-{index}", source)
    monkeypatch.setattr(session_evidence, "_SEARCH_TOTAL_BYTES", 1)

    result = session_evidence.search("absent", session_limit=10)

    assert result["sessions_scanned"] == 1
    assert result["partial"] is True


def test_session_evidence_api_requires_auth_and_validates_search(tmp_path):
    source = tmp_path / "claude.jsonl"
    _write_jsonl(source, [{
        "timestamp": AT,
        "type": "user",
        "message": {"role": "user", "content": "Review attribution"},
    }])
    session_id = _session("claude", "claude-api", source)
    client = TestClient(app)

    assert client.get(f"/work-ledger/sessions/{session_id}/evidence").status_code == 401
    evidence = client.get(
        f"/work-ledger/sessions/{session_id}/evidence",
        headers=AUTH,
    )
    search = client.get(
        "/work-ledger/session-search?q=attribution&provider=claude",
        headers=AUTH,
    )
    sessions = client.get(
        "/work-ledger/sessions?review_hints=true",
        headers=AUTH,
    )
    short = client.get("/work-ledger/session-search?q=a", headers=AUTH)
    invalid = client.get(
        "/work-ledger/session-search?q=attribution&provider=cursor",
        headers=AUTH,
    )

    assert evidence.status_code == 200
    assert evidence.json()["items"][0]["summary"] == "Review attribution"
    assert search.status_code == 200
    assert search.json()["matches"][0]["session_id"] == session_id
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["first_prompt"] == "Review attribution"
    assert short.status_code == 400
    assert invalid.status_code == 400
    assert client.get(
        "/work-ledger/sessions/missing/evidence",
        headers=AUTH,
    ).status_code == 404
