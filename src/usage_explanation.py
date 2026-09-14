"""Explain locally observed Claude and Codex usage by activity category."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.cost_estimates import price_usage_event


EFFECTIVE_WEIGHTS = {
    "input_tokens": 1.0,
    "output_tokens": 5.0,
    "cache_read_tokens": 0.1,
    "cache_write_5m_tokens": 2.0,
    "cache_write_1h_tokens": 2.0,
}

CATEGORY_LABELS = {
    "instructions": "Instructions",
    "conversation": "Conversation",
    "model_output": "Model output",
    "browser": "Browser",
    "app_control": "App control",
    "web_research": "Web research",
    "files": "Files",
    "shell": "Shell",
    "subagents": "Subagents",
    "compaction": "Compaction",
    "orchestration": "Agent control",
    "unclassified": "Other",
}

CATEGORY_COLORS = {
    "instructions": "#7D8590",
    "conversation": "#4C8DFF",
    "model_output": "#34B27B",
    "browser": "#33A7C8",
    "app_control": "#4E86A6",
    "web_research": "#E38B3A",
    "files": "#3DA58A",
    "shell": "#D0A52B",
    "subagents": "#D85D6A",
    "compaction": "#B96BAA",
    "orchestration": "#A07A45",
    "unclassified": "#656D76",
}

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "reasoning_tokens",
)


def new_parser_state() -> dict:
    return {
        "context_weights": {},
        "pending_counts": {},
        "tool_categories": {},
        "baseline_tokens": 0,
        "compaction_generation": 0,
        "is_subagent": False,
    }


def restore_parser_state(raw: object) -> dict:
    state = new_parser_state()
    if not isinstance(raw, dict):
        return state
    for key in ("context_weights", "pending_counts"):
        values = raw.get(key)
        if isinstance(values, dict):
            state[key] = {
                str(name): max(int(value or 0), 0)
                for name, value in values.items()
                if isinstance(value, (int, float))
            }
    tools = raw.get("tool_categories")
    if isinstance(tools, dict):
        state["tool_categories"] = {
            str(key): (
                str(value)
                if isinstance(value, str)
                else {
                    str(category): max(int(weight or 0), 1)
                    for category, weight in value.items()
                    if isinstance(weight, (int, float))
                }
            )
            for key, value in tools.items()
            if isinstance(value, (str, dict))
        }
    state["baseline_tokens"] = max(int(raw.get("baseline_tokens") or 0), 0)
    state["compaction_generation"] = max(int(raw.get("compaction_generation") or 0), 0)
    state["is_subagent"] = bool(raw.get("is_subagent"))
    return state


def persist_parser_state(state: dict) -> dict:
    return {
        key: state.get(key, default)
        for key, default in (
            ("context_weights", {}),
            ("pending_counts", {}),
            ("tool_categories", {}),
            ("baseline_tokens", 0),
            ("compaction_generation", 0),
            ("is_subagent", False),
        )
    }


def _estimate_tokens(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return max((len(value) + 3) // 4, 1) if value else 0
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, list):
        return sum(_estimate_tokens(item) for item in value) + len(value)
    if isinstance(value, dict):
        return sum(_estimate_tokens(key) + _estimate_tokens(item) for key, item in value.items())
    return _estimate_tokens(str(value))


def _tool_key(call_id: object) -> str | None:
    if not isinstance(call_id, str) or not call_id:
        return None
    return hashlib.sha256(call_id.encode()).hexdigest()[:24]


def _connector_category(name: str) -> str | None:
    lower = name.lower()
    if lower.startswith("mcp__"):
        parts = lower.split("__")
        if len(parts) > 2 and parts[1]:
            connector = parts[1].replace("plugin_", "").replace("-", "_")
            if connector.startswith("loop_bridge"):
                connector = "loop_bridge"
            if "chrome" in connector or "browser" in connector:
                return "browser"
            if connector == "computer_use":
                return "app_control"
            if connector in {"node_repl", "repl"}:
                return "shell"
            return f"connector:{connector}"
    return None


def tool_category(name: object) -> str:
    raw = str(name or "").strip()
    lower = raw.lower()
    connector = _connector_category(raw)
    if connector:
        return connector
    if any(term in lower for term in ("browser", "chrome", "computer_use")):
        return "app_control" if "computer_use" in lower else "browser"
    if lower in {"websearch", "webfetch", "web_search", "web_fetch", "web__run"} or (
        "web" in lower and any(term in lower for term in ("search", "fetch"))
    ):
        return "web_research"
    if any(term in lower for term in (
        "spawn_agent", "send_input", "sub_agent", "subagent", "wait_agent",
        "send_message", "followup_task", "list_agents",
    )):
        return "subagents"
    if lower in {"task", "agent"}:
        return "subagents"
    if any(term in lower for term in ("exec_command", "write_stdin", "bash", "shell", "terminal")):
        return "shell"
    if lower == "wait":
        return "shell"
    if any(term in lower for term in (
        "apply_patch", "read", "write", "edit", "glob", "grep", "notebook",
        "view_image", "file", "workspace",
    )):
        return "files"
    if any(term in lower for term in (
        "update_plan", "request_user_input", "tool_search", "goal", "memory",
        "status", "yield_control", "notify",
    )):
        return "orchestration"
    if lower.startswith("mcp"):
        return "connector:unknown"
    return "unclassified"


def category_label(category: str) -> str:
    if category.startswith("connector:"):
        name = category.split(":", 1)[1].replace("_", " ").strip()
        return f"Connector: {name.title() if name else 'Unknown'}"
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def category_color(category: str) -> str:
    if category.startswith("connector:"):
        return "#8A70C9"
    return CATEGORY_COLORS.get(category, CATEGORY_COLORS["unclassified"])


def _add_artifact(
    state: dict,
    category: str,
    value: object,
    *,
    event_count: int = 0,
) -> None:
    if state.get("is_subagent"):
        category = "subagents"
    tokens = _estimate_tokens(value)
    if tokens:
        weights = state.setdefault("context_weights", {})
        weights[category] = int(weights.get(category, 0)) + tokens
    if event_count:
        counts = state.setdefault("pending_counts", {})
        counts[category] = int(counts.get(category, 0)) + event_count


def _remember_tool(state: dict, call_id: object, category: str | dict[str, int]) -> None:
    key = _tool_key(call_id)
    if key:
        state.setdefault("tool_categories", {})[key] = category


def _take_tool_category(state: dict, call_id: object) -> str | dict[str, int]:
    key = _tool_key(call_id)
    if not key:
        return "unclassified"
    return state.setdefault("tool_categories", {}).pop(key, "unclassified")


def _tool_categories(name: object, arguments: object) -> str | dict[str, int]:
    raw_name = str(name or "").strip().lower()
    if raw_name != "exec" or not isinstance(arguments, str):
        return tool_category(name)
    nested: dict[str, int] = defaultdict(int)
    for nested_name in re.findall(r"tools\.([A-Za-z0-9_]+)", arguments):
        category = tool_category(nested_name)
        nested[category] += 1
    if nested:
        return dict(nested)
    if "ALL_TOOLS" in arguments:
        return "orchestration"
    return "unclassified"


def _add_weighted_artifact(
    state: dict,
    categories: str | dict[str, int],
    value: object,
    *,
    event_count: bool = False,
) -> None:
    if isinstance(categories, str):
        _add_artifact(state, categories, value, event_count=1 if event_count else 0)
        return
    total_tokens = _estimate_tokens(value)
    for category, tokens in _allocate_integer(total_tokens, categories).items():
        if state.get("is_subagent"):
            category = "subagents"
        weights = state.setdefault("context_weights", {})
        weights[category] = int(weights.get(category, 0)) + tokens
    if event_count:
        counts = state.setdefault("pending_counts", {})
        for category, count in categories.items():
            if state.get("is_subagent"):
                category = "subagents"
            counts[category] = int(counts.get(category, 0)) + max(int(count or 0), 1)


def _allocate_integer(amount: int, weights: dict[str, int]) -> dict[str, int]:
    amount = max(int(amount or 0), 0)
    positive = {key: max(int(value), 0) for key, value in weights.items() if int(value) > 0}
    if amount <= 0:
        return {}
    if not positive:
        return {"unclassified": amount}
    total = sum(positive.values())
    exact = {key: amount * value / total for key, value in positive.items()}
    allocated = {key: int(value) for key, value in exact.items()}
    remainder = amount - sum(allocated.values())
    for key in sorted(exact, key=lambda item: (exact[item] - allocated[item], item), reverse=True)[:remainder]:
        allocated[key] += 1
    return {key: value for key, value in allocated.items() if value}


def allocate_usage(
    state: dict,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> dict[str, dict]:
    if state.get("is_subagent"):
        categories = {"subagents": 1}
    else:
        context_total = max(
            int(input_tokens or 0)
            + int(cache_read_tokens or 0)
            + int(cache_write_5m_tokens or 0)
            + int(cache_write_1h_tokens or 0),
            0,
        )
        dynamic = {
            key: max(int(value or 0), 0)
            for key, value in state.get("context_weights", {}).items()
            if key != "instructions" and int(value or 0) > 0
        }
        inferred = max(context_total - sum(dynamic.values()), 0)
        state["baseline_tokens"] = max(int(state.get("baseline_tokens") or 0), inferred)
        categories = dict(dynamic)
        if state["baseline_tokens"]:
            categories["instructions"] = state["baseline_tokens"]
        if not categories:
            categories = {"unclassified": 1}

    result: dict[str, dict] = defaultdict(lambda: {field: 0 for field in TOKEN_FIELDS} | {"event_count": 0})
    for field, amount in (
        ("input_tokens", input_tokens),
        ("cache_read_tokens", cache_read_tokens),
        ("cache_write_5m_tokens", cache_write_5m_tokens),
        ("cache_write_1h_tokens", cache_write_1h_tokens),
    ):
        for category, value in _allocate_integer(int(amount or 0), categories).items():
            result[category][field] += value

    output_category = "subagents" if state.get("is_subagent") else "model_output"
    result[output_category]["output_tokens"] += max(int(output_tokens or 0), 0)
    result[output_category]["reasoning_tokens"] += max(int(reasoning_tokens or 0), 0)
    if output_tokens or reasoning_tokens:
        result[output_category]["event_count"] += 1
    for category, count in state.get("pending_counts", {}).items():
        result[category]["event_count"] += max(int(count or 0), 0)
    state["pending_counts"] = {}
    return dict(result)


def _claude_content(entry: dict) -> list[dict]:
    message = entry.get("message") or {}
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str) and content:
        return [{"type": "text", "text": content}]
    return []


def observe_claude_entry(entry: dict, state: dict, *, before_usage: bool) -> None:
    entry_type = str(entry.get("type") or "")
    if entry_type == "system" and entry.get("subtype") == "compact_boundary":
        if not before_usage:
            return
        state["context_weights"] = {}
        state["tool_categories"] = {}
        state["compaction_generation"] = int(state.get("compaction_generation") or 0) + 1
        _add_artifact(state, "compaction", entry.get("content"), event_count=1)
        return
    if entry_type == "attachment":
        if not before_usage:
            return
        attachment = entry.get("attachment") or {}
        attachment_type = str(attachment.get("type") or "") if isinstance(attachment, dict) else ""
        category = "files" if attachment_type in {"file", "edited_text_file", "compact_file_reference"} else "instructions"
        _add_artifact(state, category, attachment)
        return
    if entry_type not in {"user", "assistant"}:
        return
    if entry_type == "assistant" and before_usage:
        return
    if entry_type == "user" and not before_usage:
        return

    blocks = _claude_content(entry)
    for block in blocks:
        block_type = block.get("type")
        if block_type == "tool_use":
            category = tool_category(block.get("name"))
            _remember_tool(state, block.get("id"), category)
            _add_artifact(
                state,
                category,
                {"name": block.get("name"), "input": block.get("input")},
                event_count=1,
            )
        elif block_type == "tool_result":
            category = _take_tool_category(state, block.get("tool_use_id"))
            _add_artifact(state, category, block.get("content"))
        elif entry.get("isCompactSummary"):
            _add_artifact(state, "compaction", block)
        elif entry_type == "user":
            _add_artifact(state, "conversation", block, event_count=1)
        elif block_type in {"text", "thinking"}:
            _add_artifact(state, "model_output", block)


def observe_codex_entry(entry: dict, state: dict) -> None:
    entry_type = str(entry.get("type") or "")
    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return
    payload_type = str(payload.get("type") or "")
    if entry_type == "session_meta":
        thread_source = json.dumps(payload.get("thread_source") or "").lower()
        if "subagent" in thread_source or "sub_agent" in thread_source:
            state["is_subagent"] = True
        _add_artifact(
            state,
            "instructions",
            {
                "base_instructions": payload.get("base_instructions"),
                "dynamic_tools": payload.get("dynamic_tools"),
            },
        )
        return
    if entry_type == "compacted" or payload_type == "context_compacted":
        state["context_weights"] = {}
        state["tool_categories"] = {}
        state["compaction_generation"] = int(state.get("compaction_generation") or 0) + 1
        _add_artifact(
            state,
            "compaction",
            payload.get("replacement_history") or payload.get("message"),
            event_count=1,
        )
        return
    if payload_type in {"function_call", "custom_tool_call"}:
        arguments = payload.get("arguments", payload.get("input"))
        categories = _tool_categories(payload.get("name"), arguments)
        _remember_tool(state, payload.get("call_id"), categories)
        _add_weighted_artifact(
            state,
            categories,
            {
                "name": payload.get("name"),
                "arguments": arguments,
            },
            event_count=True,
        )
        return
    if payload_type in {"function_call_output", "custom_tool_call_output"}:
        categories = _take_tool_category(state, payload.get("call_id"))
        _add_weighted_artifact(state, categories, payload.get("output"))
        return
    if payload_type in {"web_search_call", "web_search_end"}:
        _add_artifact(state, "web_research", payload, event_count=1)
        return
    if payload_type == "mcp_tool_call_end":
        invocation = payload.get("invocation") or {}
        name = (
            invocation.get("server") or invocation.get("tool")
            if isinstance(invocation, dict)
            else None
        )
        _add_artifact(state, tool_category(f"mcp__{name or 'unknown'}__tool"), payload.get("result"), event_count=1)
        return
    if payload_type == "sub_agent_activity":
        _add_artifact(state, "subagents", payload.get("kind"), event_count=1)
        return
    if payload_type in {"user_message"}:
        _add_artifact(state, "conversation", payload.get("message"), event_count=1)
        return
    if payload_type == "message":
        role = payload.get("role")
        category = "conversation" if role == "user" else "model_output"
        _add_artifact(state, category, payload.get("content"), event_count=1 if role == "user" else 0)
        return
    if payload_type in {"agent_message", "agent_reasoning", "reasoning"}:
        _add_artifact(state, "model_output", payload.get("message") or payload.get("text") or payload.get("summary"))


def _window(period: str, start_at: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    if period not in {"day", "week"}:
        raise ValueError("period must be day or week")
    if start_at is not None:
        return start_at.astimezone(timezone.utc), now
    if period == "week":
        return now - timedelta(days=7), now
    local_now = now.astimezone()
    return datetime(local_now.year, local_now.month, local_now.day).astimezone(timezone.utc), now


def _empty_category() -> dict:
    return {field: 0 for field in TOKEN_FIELDS} | {
        "event_count": 0,
        "effective_tokens": 0.0,
        "estimated_cost_usd": 0.0,
        "estimated_credits": 0.0,
        "priced_tokens": 0,
        "models": set(),
        "unpriced_models": set(),
        "compaction_generations": set(),
    }


def _effective(values: dict) -> float:
    return sum(float(values.get(field, 0)) * weight for field, weight in EFFECTIVE_WEIGHTS.items())


def build_usage_explanation(
    provider: str,
    period: str,
    *,
    start_at: datetime | None = None,
    now: datetime | None = None,
    sync: bool = True,
) -> dict:
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    from src import usage_ledger

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start, end = _window(period, start_at, now)
    if sync:
        usage_ledger.sync_provider(provider)

    categories: dict[str, dict] = defaultdict(_empty_category)
    sessions: set[str] = set()
    for event in usage_ledger.iter_events_between(provider, start, end):
        sessions.add(str(event["session_id"]))
        breakdown = event.get("usage_breakdown") or {}
        generation = int(event.get("compaction_generation") or 0)
        for category, values in breakdown.items():
            if not isinstance(values, dict):
                continue
            target = categories[str(category)]
            for field in TOKEN_FIELDS:
                target[field] += int(values.get(field) or 0)
            target["event_count"] += int(values.get("event_count") or 0)
            target["models"].add(str(event["model"]))
            if generation:
                target["compaction_generations"].add((event["session_id"], generation))
            priced = price_usage_event(
                provider,
                str(event["model"]),
                event["at"],
                input_tokens=int(values.get("input_tokens") or 0),
                output_tokens=int(values.get("output_tokens") or 0),
                cache_read_tokens=int(values.get("cache_read_tokens") or 0),
                cache_write_5m_tokens=int(values.get("cache_write_5m_tokens") or 0),
                cache_write_1h_tokens=int(values.get("cache_write_1h_tokens") or 0),
                reasoning_tokens=int(values.get("reasoning_tokens") or 0),
            )
            target["effective_tokens"] += _effective(values)
            target["priced_tokens"] += int(priced["priced_tokens"])
            if priced["estimated_cost_usd"] is not None:
                target["estimated_cost_usd"] += float(priced["estimated_cost_usd"])
            elif priced["total_tokens"] > 0:
                target["unpriced_models"].add(str(event["model"]))
            if priced["estimated_credits"] is not None:
                target["estimated_credits"] += float(priced["estimated_credits"])

    total_effective = sum(row["effective_tokens"] for row in categories.values())
    total_tokens = sum(
        row["input_tokens"] + row["output_tokens"] + row["cache_read_tokens"]
        + row["cache_write_5m_tokens"] + row["cache_write_1h_tokens"]
        for row in categories.values()
    )
    rows = []
    for category, values in categories.items():
        category_tokens = (
            values["input_tokens"] + values["output_tokens"] + values["cache_read_tokens"]
            + values["cache_write_5m_tokens"] + values["cache_write_1h_tokens"]
        )
        confidence = "low" if category in {"instructions", "unclassified"} else "medium"
        if category in {"model_output", "subagents"}:
            confidence = "high"
        rows.append({
            "id": category,
            "label": category_label(category),
            "color": category_color(category),
            **{field: int(values[field]) for field in TOKEN_FIELDS},
            "total_tokens": category_tokens,
            "effective_tokens": round(values["effective_tokens"], 1),
            "share_pct": round(values["effective_tokens"] / total_effective * 100, 1) if total_effective else 0.0,
            "event_count": int(values["event_count"]),
            "estimated_cost_usd": round(values["estimated_cost_usd"], 6) if values["priced_tokens"] else None,
            "estimated_credits": round(values["estimated_credits"], 3) if provider == "codex" and values["priced_tokens"] else None,
            "pricing_coverage_pct": round(values["priced_tokens"] / category_tokens * 100, 1) if category_tokens else None,
            "models": sorted(values["models"]),
            "unpriced_models": sorted(values["unpriced_models"]),
            "confidence": confidence,
        })
    rows.sort(key=lambda row: (-row["effective_tokens"], row["label"]))

    totals = _empty_category()
    for values in categories.values():
        for field in TOKEN_FIELDS:
            totals[field] += values[field]
        for field in ("event_count", "priced_tokens"):
            totals[field] += values[field]
        totals["effective_tokens"] += values["effective_tokens"]
        totals["estimated_cost_usd"] += values["estimated_cost_usd"]
        totals["estimated_credits"] += values["estimated_credits"]
        totals["unpriced_models"].update(values["unpriced_models"])
        totals["compaction_generations"].update(values["compaction_generations"])
    unclassified_tokens = next((row["total_tokens"] for row in rows if row["id"] == "unclassified"), 0)
    compactions = sum(row["event_count"] for row in rows if row["id"] == "compaction")
    return {
        "provider": provider,
        "period": period,
        "window_start_at": start.isoformat(),
        "window_end_at": end.isoformat(),
        "estimate_basis": "local_transcript_attribution",
        "quota_relation": "independent_estimate",
        "coverage": {
            "status": "compacted" if compactions else "complete",
            "confidence": "medium" if rows else "unavailable",
            "compactions": compactions,
            "classified_pct": round((total_tokens - unclassified_tokens) / total_tokens * 100, 1) if total_tokens else None,
        },
        "totals": {
            **{field: int(totals[field]) for field in TOKEN_FIELDS},
            "total_tokens": total_tokens,
            "effective_tokens": round(total_effective, 1),
            "event_count": int(totals["event_count"]),
            "sessions": len(sessions),
            "estimated_cost_usd": round(totals["estimated_cost_usd"], 6) if totals["priced_tokens"] else None,
            "estimated_credits": round(totals["estimated_credits"], 3) if provider == "codex" and totals["priced_tokens"] else None,
            "pricing_coverage_pct": round(totals["priced_tokens"] / total_tokens * 100, 1) if total_tokens else None,
            "unpriced_models": sorted(totals["unpriced_models"]),
        },
        "categories": rows,
    }
