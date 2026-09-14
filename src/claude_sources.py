"""Normalize Claude session surfaces and stable event identities."""

from __future__ import annotations

from pathlib import Path


def claude_surface_for_path(path: Path) -> str:
    normalized = str(path)
    if "Library/Application Support/Claude/local-agent-mode-sessions/" in normalized:
        return "desktop"
    return "claude-code"


def claude_event_identity(entry: dict) -> str | None:
    """Return an identity that survives Claude session forks and file copies."""
    message = entry.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    message_id = message.get("id") or entry.get("requestId")
    if message_id:
        return f"claude-message:{message_id}"
    event_id = entry.get("uuid")
    if event_id:
        return f"claude-event:{event_id}"
    return None
