"""The liveness probe must never signal a process on Windows.

On Windows, os.kill(pid, 0) is TerminateProcess. The PIDs session_runtime probes come from
~/.claude/sessions/*.json: the user's own live Claude Code sessions.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

from src import process_liveness, session_runtime

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src", "clients", "packaging")
PROBE_FILE = ROOT / "src" / "process_liveness.py"


class FakeKernel32:
    """OpenProcess/WaitForSingleObject for a set of 'running' pids."""

    def __init__(self, running):
        self.running = set(running)
        self.closed = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Win32 name
        assert access == 0x00100000, "must ask only for SYNCHRONIZE"
        return pid if pid in self.running else 0

    def WaitForSingleObject(self, handle, timeout):  # noqa: N802
        assert timeout == 0
        return 0x00000102  # WAIT_TIMEOUT: still running

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be Windows; record any os.kill instead of sending it."""
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    kernel32 = FakeKernel32(running={4242})
    monkeypatch.setattr(process_liveness, "_kernel32", lambda: kernel32)
    return calls, kernel32


def test_session_probe_never_calls_os_kill_on_windows(windows):
    calls, _ = windows
    assert session_runtime._process_is_running(4242) is True
    assert session_runtime._process_is_running(999) is False
    assert calls == [], f"os.kill called on Windows (TerminateProcess): {calls}"


def test_live_claude_sessions_on_windows_does_not_signal(windows, tmp_path):
    calls, kernel32 = windows
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "a.json").write_text('{"sessionId": "live", "pid": 4242}')
    (sessions / "b.json").write_text('{"sessionId": "gone", "pid": 7}')
    live = session_runtime.live_claude_sessions(home=tmp_path)
    assert set(live) == {"live"}
    assert calls == []
    assert kernel32.closed == [4242]


def test_pid_alive_windows_branch(windows):
    calls, _ = windows
    assert process_liveness.pid_alive(4242)
    assert not process_liveness.pid_alive(1)
    assert not process_liveness.pid_alive(0)
    assert not process_liveness.pid_alive("not a pid")
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX branch")
def test_pid_alive_posix_branch():
    assert process_liveness.pid_alive(os.getpid())
    assert not process_liveness.pid_alive(0)
    assert not process_liveness.pid_alive(None)


def test_supervisor_uses_the_shared_probe():
    sys.path.insert(0, str(ROOT / "packaging" / "backend"))
    try:
        import aicur_backend
    finally:
        sys.path.remove(str(ROOT / "packaging" / "backend"))
    assert aicur_backend.pid_alive is process_liveness.pid_alive


# ── Source-tree guard ────────────────────────────────────────────────────────

def kill_zero_calls(tree: ast.AST) -> list[ast.Call]:
    """Every os.kill(<x>, 0) / kill(<x>, 0) (from os import kill) / <alias>.kill(<x>, 0)."""
    os_aliases = {"os"}
    kill_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "kill":
                    kill_names.add(alias.asname or "kill")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        is_kill = (
            isinstance(func, ast.Attribute) and func.attr == "kill"
            and isinstance(func.value, ast.Name) and func.value.id in os_aliases
        ) or (isinstance(func, ast.Name) and func.id in kill_names)
        sig = node.args[1]
        if is_kill and isinstance(sig, ast.Constant) and sig.value == 0:
            found.append(node)
    return found


def scan(root: Path, dirs=SCANNED) -> dict[Path, list[int]]:
    hits = {}
    for top in dirs:
        for path in sorted((root / top).rglob("*.py")):
            calls = kill_zero_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            if calls:
                hits[path] = [c.lineno for c in calls]
    return hits


def test_guard_finds_a_planted_violation(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "planted.py").write_text(
        "import os as o\nfrom os import kill as k\n\ndef f(p):\n    o.kill(p, 0)\n    k(p, 0)\n")
    hits = scan(tmp_path, dirs=("src",))
    assert hits == {tmp_path / "src" / "planted.py": [5, 6]}


def test_only_the_shared_probe_calls_os_kill_zero():
    hits = scan(ROOT)
    assert set(hits) == {PROBE_FILE}, f"os.kill(<pid>, 0) outside the shared probe: {hits}"
    assert len(hits[PROBE_FILE]) == 1
    # ...and that one call sits after the win32 early-return inside pid_alive.
    tree = ast.parse(PROBE_FILE.read_text(encoding="utf-8"))
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pid_alive")
    win32_branch = next(
        n for n in func.body
        if isinstance(n, ast.If) and "win32" in ast.unparse(n.test)
    )
    last = win32_branch.body[-1]  # the win32 branch always returns before the POSIX code
    assert isinstance(last, ast.Try) and isinstance(last.body[-1], ast.Return)
    assert hits[PROBE_FILE][0] > win32_branch.end_lineno
