import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src import usage_explanation, usage_ledger


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(path))
    usage_ledger._initialized_paths.discard(str(path))
    return path


def _token_sums(breakdown: dict) -> dict:
    return {
        field: sum(int(values.get(field) or 0) for values in breakdown.values())
        for field in usage_explanation.TOKEN_FIELDS
    }


def test_claude_breakdown_conserves_token_classes_and_tracks_tools():
    state = usage_explanation.new_parser_state()
    user = {
        "type": "user",
        "message": {"role": "user", "content": "Inspect the failing build"},
    }
    usage_explanation.observe_claude_entry(user, state, before_usage=True)
    first = usage_explanation.allocate_usage(
        state,
        input_tokens=100,
        output_tokens=30,
        cache_read_tokens=200,
        cache_write_5m_tokens=40,
    )
    assistant = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Bash",
                "input": {"command": "swift test"},
            }],
        },
    }
    usage_explanation.observe_claude_entry(assistant, state, before_usage=False)
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "test output",
            }],
        },
    }
    usage_explanation.observe_claude_entry(result, state, before_usage=True)
    second = usage_explanation.allocate_usage(
        state,
        input_tokens=80,
        output_tokens=20,
        cache_read_tokens=300,
    )

    assert _token_sums(first) == {
        "input_tokens": 100,
        "output_tokens": 30,
        "cache_read_tokens": 200,
        "cache_write_5m_tokens": 40,
        "cache_write_1h_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert _token_sums(second)["input_tokens"] == 80
    assert _token_sums(second)["cache_read_tokens"] == 300
    assert second["shell"]["event_count"] == 1
    assert second["model_output"]["output_tokens"] == 20
    assert "instructions" in first


def test_codex_breakdown_labels_connectors_and_compaction():
    state = usage_explanation.new_parser_state()
    usage_explanation.observe_codex_entry({
        "type": "session_meta",
        "payload": {"base_instructions": "system", "dynamic_tools": ["tools"]},
    }, state)
    usage_explanation.observe_codex_entry({
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "call_id": "call-1",
            "name": "mcp__github__get_pull_request",
            "input": {"number": 10},
        },
    }, state)
    usage_explanation.observe_codex_entry({
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "call-1",
            "output": "pull request metadata",
        },
    }, state)
    breakdown = usage_explanation.allocate_usage(
        state,
        input_tokens=60,
        output_tokens=10,
        cache_read_tokens=140,
        reasoning_tokens=4,
    )
    usage_explanation.observe_codex_entry({
        "type": "compacted",
        "payload": {"replacement_history": "summary"},
    }, state)

    assert "connector:github" in breakdown
    assert breakdown["connector:github"]["event_count"] == 1
    assert breakdown["model_output"]["reasoning_tokens"] == 4
    assert state["compaction_generation"] == 1
    assert set(state["context_weights"]) == {"compaction"}


def test_codex_tool_catalog_discovery_is_agent_control():
    state = usage_explanation.new_parser_state()
    usage_explanation.observe_codex_entry({
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "call_id": "catalog-call",
            "name": "exec",
            "input": "const matches = ALL_TOOLS.filter(tool => tool.name); text(matches);",
        },
    }, state)
    usage_explanation.observe_codex_entry({
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "catalog-call",
            "output": "available tools",
        },
    }, state)

    breakdown = usage_explanation.allocate_usage(state, input_tokens=100)

    assert breakdown["orchestration"]["input_tokens"] > 0
    assert _token_sums(breakdown)["input_tokens"] == 100
    assert breakdown["orchestration"]["event_count"] == 1
    assert "unclassified" not in breakdown


def test_build_usage_explanation_aggregates_pricing_and_coverage(activity_db):
    usage_ledger.init()
    at = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    breakdown = {
        "files": {
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_read_tokens": 50,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "reasoning_tokens": 0,
            "event_count": 2,
        },
        "model_output": {
            "input_tokens": 0,
            "output_tokens": 25,
            "cache_read_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "reasoning_tokens": 5,
            "event_count": 1,
        },
    }
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO usage_events(
                   provider, file_key, line_offset, path, timestamp_us, day,
                   session_id, model, input_tokens, output_tokens, cache_read_tokens,
                   reasoning_tokens, usage_breakdown_json, compaction_generation
               ) VALUES ('claude', 'file', 1, '/session.jsonl', ?, '2026-07-25',
                         'session', 'claude-fable-5', 100, 25, 50, 5, ?, 0)""",
            (round(at.timestamp() * 1_000_000), json.dumps(breakdown)),
        )

    report = usage_explanation.build_usage_explanation(
        "claude",
        "day",
        start_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        now=datetime(2026, 7, 25, 13, tzinfo=timezone.utc),
        sync=False,
    )

    assert report["totals"]["total_tokens"] == 175
    assert report["totals"]["effective_tokens"] == 230
    assert report["totals"]["sessions"] == 1
    assert [row["id"] for row in report["categories"]] == ["model_output", "files"]
    assert report["coverage"]["classified_pct"] == 100
    assert report["quota_relation"] == "independent_estimate"


def test_subagent_state_collapses_categories():
    state = usage_explanation.new_parser_state()
    state["is_subagent"] = True
    usage_explanation.observe_codex_entry({
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "1", "name": "exec_command", "arguments": "pwd"},
    }, state)
    breakdown = usage_explanation.allocate_usage(state, input_tokens=10, output_tokens=5)

    assert set(breakdown) == {"subagents"}
    assert breakdown["subagents"]["input_tokens"] == 10
    assert breakdown["subagents"]["output_tokens"] == 5
