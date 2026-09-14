"""Read-only, bounded evidence views over local Claude and Codex sessions."""

from __future__ import annotations

from collections import OrderedDict, deque
import json
import re
import threading
from pathlib import Path

from src import usage_ledger, work_activity, work_ledger


_DETAIL_BYTES = 16 * 1024 * 1024
_FIRST_PROMPT_BYTES = 1024 * 1024
_SEARCH_BYTES = 4 * 1024 * 1024
_SEARCH_TOTAL_BYTES = 64 * 1024 * 1024
_DETAIL_ITEMS = 800
_SEARCH_ITEMS = 400
_MAX_CACHE_ENTRIES = 64
_MAX_SUMMARY_CHARS = 800
_MAX_FIRST_PROMPT_CHARS = 280
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SYSTEM_BLOCK_RE = re.compile(
    r"<(?P<tag>system-reminder|local-command-caveat|local-command-stdout|"
    r"local-command-stderr|command-message|command-args|recommended_plugins|"
    r"recommended_skills|scheduled-task|environment_context)[^>]*>.*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_COMMAND_TAG_RE = re.compile(r"<command-name>.*?</command-name>", re.IGNORECASE | re.DOTALL)
_TRANSCRIPT_DELTA_RE = re.compile(
    r"<transcript_delta>.*?</transcript_delta>",
    re.IGNORECASE | re.DOTALL,
)
_PROMPT_WRAPPER_RE = re.compile(
    r"</?(?:realtime_delegation|input)[^>]*>",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_API_TOKEN_RE = re.compile(r"\b(?:sk|sess)-[A-Za-z0-9_-]{12,}\b")
_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$", re.MULTILINE)
_FILE_KEYS = ("file_path", "path", "notebook_path", "target_file")

_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()


def _safe_text(value: object, *, limit: int = _MAX_SUMMARY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    text = _ANSI_RE.sub("", value)
    text = _SYSTEM_BLOCK_RE.sub(" ", text)
    text = _COMMAND_TAG_RE.sub(" ", text)
    text = _TRANSCRIPT_DELTA_RE.sub(" ", text)
    text = _PROMPT_WRAPPER_RE.sub(" ", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _API_TOKEN_RE.sub("<redacted>", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


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


def _content_text(content: object, allowed_types: set[str]) -> str:
    if isinstance(content, str):
        return _safe_text(content)
    if not isinstance(content, list):
        return ""
    values: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "text")
        if block_type not in allowed_types:
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str):
            values.append(text)
    return _safe_text("\n".join(values))


def _file_paths(data: dict, raw: object = None) -> list[str]:
    paths: list[str] = []
    for key in _FILE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    if isinstance(raw, str):
        paths.extend(match.strip() for match in _PATCH_PATH_RE.findall(raw))
    return list(dict.fromkeys(paths))[:12]


def _tool_evidence(name: object, raw_input: object) -> dict:
    raw_name = str(name or "tool").strip()
    tool_name = work_activity._safe_tool_name(name)
    normalized = tool_name.lower()
    mcp_action = None
    if raw_name.lower().startswith("mcp__"):
        mcp_action = raw_name.rsplit("__", 1)[-1].replace("_", " ").strip()
    data = _json_object(raw_input)
    command = data.get("cmd") or data.get("command")
    if not command and isinstance(raw_input, str) and normalized in {
        "exec", "exec_command", "bash", "shell",
    }:
        command = raw_input
    paths = _file_paths(data, raw_input)

    if command is not None:
        category = work_activity._command_category(str(command))
        titles = {
            "test": "Ran tests",
            "build": "Built project",
            "git": "Ran Git command",
            "tool": "Ran command",
        }
        kind = category if category != "tool" else "command"
        return {
            "kind": kind,
            "title": titles[category],
            "summary": _safe_text(str(command)),
            "tool_name": tool_name,
            "command_category": category,
            "file_paths": paths,
        }

    if normalized in work_activity._EDIT_TOOLS or normalized in {"applypatch", "apply_patch"}:
        target = Path(paths[0]).name if paths else None
        return {
            "kind": "edit",
            "title": f"Edited {target}" if target else "Edited files",
            "summary": _safe_text(paths[0]) if paths else tool_name,
            "tool_name": tool_name,
            "command_category": "edit",
            "file_paths": paths,
        }

    if normalized in {"read", "view", "view_image"} and paths:
        return {
            "kind": "read",
            "title": f"Read {Path(paths[0]).name}",
            "summary": _safe_text(paths[0]),
            "tool_name": tool_name,
            "command_category": "read",
            "file_paths": paths,
        }

    query = data.get("query") or data.get("q") or data.get("pattern")
    if "web" in normalized and "search" in normalized:
        return {
            "kind": "web",
            "title": "Searched the web",
            "summary": _safe_text(query) or tool_name,
            "tool_name": tool_name,
            "command_category": "web",
            "file_paths": paths,
        }
    if "web" in normalized and "fetch" in normalized:
        return {
            "kind": "web",
            "title": "Fetched web page",
            "summary": _safe_text(data.get("url")) or tool_name,
            "tool_name": tool_name,
            "command_category": "web",
            "file_paths": paths,
        }
    if "search" in normalized or normalized in {"grep", "glob", "find"}:
        return {
            "kind": "search",
            "title": "Searched code",
            "summary": _safe_text(query) or tool_name,
            "tool_name": tool_name,
            "command_category": "search",
            "file_paths": paths,
        }

    return {
        "kind": "tool",
        "title": "Used MCP" if tool_name == "MCP" else f"Used {tool_name}",
        "summary": _safe_text(mcp_action) or tool_name,
        "tool_name": tool_name,
        "command_category": "tool",
        "file_paths": paths,
    }


def _evidence(
    *,
    provider: str,
    session_id: str,
    occurred_at_us: int,
    line_offset: int,
    item_index: int,
    kind: str,
    title: str,
    summary: str,
    tool_name: str | None = None,
    command_category: str | None = None,
    file_paths: list[str] | None = None,
) -> dict:
    evidence_id = work_ledger.stable_id(
        "evidence", provider, session_id, str(line_offset), str(item_index)
    )
    return {
        "id": evidence_id,
        "occurred_at_us": occurred_at_us,
        "kind": kind,
        "title": title,
        "summary": summary,
        "tool_name": tool_name,
        "command_category": command_category,
        "file_paths": file_paths or [],
    }


def extract_entry(
    provider: str,
    session_id: str,
    entry: dict,
    *,
    line_offset: int,
    include_source: bool = False,
) -> list[dict]:
    at = usage_ledger._timestamp(entry.get("timestamp"))
    if at is None:
        return []
    occurred_at_us = round(at.timestamp() * 1_000_000)
    descriptors: list[dict] = []

    if provider == "claude":
        message = entry.get("message") or {}
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            text = _content_text(content, {"text"})
            if text:
                descriptors.append({"kind": "prompt", "title": "Prompt", "summary": text})
        elif role == "assistant":
            text = _content_text(content, {"text"})
            if text:
                descriptors.append({"kind": "response", "title": "Response", "summary": text})
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        descriptors.append(_tool_evidence(block.get("name"), block.get("input")))

    elif provider == "codex" and entry.get("type") == "response_item":
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role")
            text = _content_text(payload.get("content"), {"input_text", "output_text", "text"})
            if role == "user" and text:
                descriptors.append({"kind": "prompt", "title": "Prompt", "summary": text})
            elif role == "assistant" and text:
                descriptors.append({"kind": "response", "title": "Response", "summary": text})
        elif payload_type in {"function_call", "custom_tool_call", "web_search_call"}:
            name = payload.get("name") or (
                "web_search" if payload_type == "web_search_call" else "tool"
            )
            raw_input = payload.get("arguments") if payload_type == "function_call" else payload.get("input")
            descriptors.append(_tool_evidence(name, raw_input))

    items: list[dict] = []
    for item_index, descriptor in enumerate(descriptors):
        if not descriptor.get("summary"):
            continue
        item = _evidence(
            provider=provider,
            session_id=session_id,
            occurred_at_us=occurred_at_us,
            line_offset=line_offset,
            item_index=item_index,
            **descriptor,
        )
        if include_source:
            item["source_line_offset"] = line_offset
            item["source_item_index"] = item_index
        items.append(item)
    return items


def _read_session(path: Path, provider: str, session_id: str, *, max_bytes: int, max_items: int) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"items": [], "truncated": False, "bytes_scanned": 0, "source_missing": True}
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size, provider, session_id, max_bytes, max_items)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            _cache.move_to_end(cache_key)
            return cached

    start = max(0, stat.st_size - max_bytes)
    items: deque[dict] = deque(maxlen=max_items)
    total_items = 0
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            scan_start = handle.tell()
            while True:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                try:
                    entry = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(entry, dict):
                    extracted = extract_entry(
                        provider,
                        session_id,
                        entry,
                        line_offset=line_offset,
                    )
                    total_items += len(extracted)
                    items.extend(extracted)
            scanned_to = handle.tell()
    except OSError:
        return {"items": [], "truncated": False, "bytes_scanned": 0, "source_missing": True}

    result = {
        "items": list(items),
        "truncated": start > 0 or scanned_to < stat.st_size or total_items > max_items,
        "bytes_scanned": max(0, scanned_to - scan_start),
        "source_missing": False,
    }
    with _cache_lock:
        _cache[cache_key] = result
        _cache.move_to_end(cache_key)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return result


def session_timeline(session_id: str, *, limit: int = _DETAIL_ITEMS) -> dict | None:
    session = work_ledger.get_ai_session_by_id(session_id)
    if session is None:
        return None
    bounded_limit = max(1, min(int(limit), _DETAIL_ITEMS))
    source_path = session.get("source_path")
    parsed = _read_session(
        Path(source_path) if source_path else Path("/__missing__"),
        str(session["provider"]),
        str(session["provider_session_id"]),
        max_bytes=_DETAIL_BYTES,
        max_items=bounded_limit,
    )
    return {
        "session_id": session_id,
        "provider": session["provider"],
        "provider_session_id": session["provider_session_id"],
        "items": parsed["items"],
        "truncated": parsed["truncated"],
        "source_missing": parsed["source_missing"],
        "bytes_scanned": parsed["bytes_scanned"],
    }


def first_prompt_preview(session: dict) -> str | None:
    source_path = session.get("source_path")
    if not source_path:
        return None
    path = Path(str(source_path))
    try:
        stat = path.stat()
    except OSError:
        return None
    provider = str(session["provider"])
    provider_session_id = str(session["provider_session_id"])
    cache_key = (
        "first_prompt",
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        provider,
        provider_session_id,
    )
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            _cache.move_to_end(cache_key)
            return cached.get("prompt")

    prompt = None
    try:
        with path.open("rb") as handle:
            while handle.tell() < _FIRST_PROMPT_BYTES:
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
                entry_session_id = entry.get("sessionId")
                if entry_session_id and str(entry_session_id) != provider_session_id:
                    continue
                prompt_item = next(
                    (
                        item for item in extract_entry(
                            provider,
                            provider_session_id,
                            entry,
                            line_offset=line_offset,
                        )
                        if item["kind"] == "prompt"
                    ),
                    None,
                )
                if prompt_item:
                    candidate = _safe_text(
                        prompt_item["summary"],
                        limit=_MAX_FIRST_PROMPT_CHARS,
                    )
                    if candidate.casefold() in {
                        "[request interrupted by user]",
                        "[user interrupted request]",
                    }:
                        continue
                    prompt = candidate or None
                    if prompt:
                        break
    except OSError:
        return None

    with _cache_lock:
        _cache[cache_key] = {"prompt": prompt}
        _cache.move_to_end(cache_key)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return prompt


def _display_name(session: dict) -> str:
    for key in ("native_title", "nickname"):
        value = str(session.get(key) or "").strip()
        if value:
            return value
    branch = str(session.get("branch") or "").strip()
    if branch and branch.lower() not in {"main", "master", "trunk", "develop", "dev", "head"}:
        return branch
    repository = str(session.get("repository_name") or "").strip()
    if repository:
        return repository
    return f"{str(session['provider']).title()} {str(session['provider_session_id'])[:8]}"


def _matches_query(query_terms: list[str], *values: object) -> bool:
    haystack = " ".join(str(value or "") for value in values).casefold()
    return all(term in haystack for term in query_terms)


def bounded_search(
    query: str,
    *,
    provider: str | None = None,
    limit: int = 50,
    session_limit: int = 500,
    sessions: list[dict] | None = None,
) -> dict:
    normalized_query = _safe_text(query, limit=200)
    terms = normalized_query.casefold().split()
    if len(normalized_query) < 2 or not terms:
        raise ValueError("query must contain at least 2 characters")
    if provider is not None and provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    bounded_limit = max(1, min(int(limit), 100))
    bounded_sessions = max(1, min(int(session_limit), 500))
    if sessions is None:
        available_sessions = work_ledger.list_ai_sessions(
            provider=provider,
            limit=bounded_sessions,
        )
        session_count_is_bounded = len(available_sessions) >= bounded_sessions
    else:
        filtered = [
            session for session in sessions
            if provider is None or session.get("provider") == provider
        ]
        session_count_is_bounded = len(filtered) > bounded_sessions
        available_sessions = filtered[:bounded_sessions]
    matches: list[dict] = []
    scanned = 0
    bytes_scanned = 0
    partial = session_count_is_bounded
    missing = 0

    for session in available_sessions:
        if len(matches) >= bounded_limit or bytes_scanned >= _SEARCH_TOTAL_BYTES:
            partial = True
            break
        source_path = session.get("source_path")
        parsed = _read_session(
            Path(source_path) if source_path else Path("/__missing__"),
            str(session["provider"]),
            str(session["provider_session_id"]),
            max_bytes=_SEARCH_BYTES,
            max_items=_SEARCH_ITEMS,
        )
        scanned += 1
        bytes_scanned += parsed["bytes_scanned"]
        partial = partial or parsed["truncated"]
        missing += int(parsed["source_missing"])
        session_name = _display_name(session)
        metadata_match = _matches_query(
            terms,
            session_name,
            session.get("repository_name"),
            session.get("branch"),
            session.get("cwd"),
            " ".join(session.get("models") or []),
        )
        session_matches = [
            item for item in parsed["items"]
            if _matches_query(
                terms,
                item["title"],
                item["summary"],
                item.get("tool_name"),
                " ".join(item.get("file_paths") or []),
            )
        ]
        if metadata_match and not session_matches:
            session_matches = [{
                "id": None,
                "occurred_at_us": session.get("source_modified_at_us")
                or session.get("ended_at_us")
                or session.get("started_at_us"),
                "kind": "session",
                "title": "Session",
                "summary": session_name,
            }]
        for item in session_matches[-3:]:
            matches.append({
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
            })
            if len(matches) >= bounded_limit:
                break

    matches.sort(key=lambda item: int(item.get("occurred_at_us") or 0), reverse=True)
    return {
        "query": normalized_query,
        "matches": matches[:bounded_limit],
        "sessions_scanned": scanned,
        "sessions_available": len(available_sessions),
        "bytes_scanned": bytes_scanned,
        "missing_sources": missing,
        "partial": partial,
    }


def search(
    query: str,
    *,
    provider: str | None = None,
    limit: int = 50,
    session_limit: int = 500,
) -> dict:
    from src import session_search_index

    return session_search_index.search(
        query,
        provider=provider,
        limit=limit,
        session_limit=session_limit,
    )
