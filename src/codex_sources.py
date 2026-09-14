"""Discover Codex session stores and normalize their client surface."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Iterable


def codex_session_roots(home: Path | None = None) -> list[Path]:
    """Return the standard store plus isolated stores created by local clients."""
    base = (home or Path.home()).expanduser()
    candidates = [base / ".codex" / "sessions"]
    candidates.extend(sorted((base / ".loop" / "runs").glob("*/*/codex-home/sessions")))

    configured = os.environ.get("USAGE_TRACKER_CODEX_SESSION_ROOTS", "")
    candidates.extend(
        Path(value).expanduser()
        for value in configured.split(os.pathsep)
        if value.strip()
    )

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        roots.append(candidate)
    return roots


def codex_session_files(home: Path | None = None) -> Iterable[Path]:
    """Yield unique Codex JSONL sessions across all known local stores."""
    seen: set[str] = set()
    for root in codex_session_roots(home):
        for path in sorted(root.glob("*/*/*/*.jsonl")):
            key = str(path)
            if key not in seen:
                seen.add(key)
                yield path


def canonical_codex_surface(originator: object, source: object) -> str:
    """Prefer the creating client over Codex's often-generic source field."""
    origin = str(originator or "").strip().lower()
    origin_map = {
        "loop": "loop",
        "codex-tui": "cli",
        "codex desktop": "desktop",
        "claude code": "claude-code",
        "codex_vscode": "vscode",
        "codex_work_desktop": "work",
        "codex_exec": "exec",
    }
    if origin in origin_map:
        return origin_map[origin]

    if isinstance(source, dict):
        if source.get("subagent") is not None:
            return "subagent"
        source_text = json.dumps(source, sort_keys=True)
    else:
        source_text = str(source or "")
    normalized = source_text.strip().lower()
    source_map = {
        "cli": "cli",
        "vscode": "vscode",
        "exec": "exec",
        "desktop": "desktop",
        "work": "work",
    }
    return source_map.get(normalized, "unknown")


def isolated_codex_default_model(session_path: Path) -> str | None:
    """Read the immutable model configured for an isolated per-run Codex home."""
    sessions_root = next(
        (parent for parent in session_path.parents if parent.name == "sessions"),
        None,
    )
    if sessions_root is None or sessions_root.parent.name != "codex-home":
        return None
    try:
        config = tomllib.loads((sessions_root.parent / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = config.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None
