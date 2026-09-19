"""Tests for clients/tray_core.py (stdlib only, no network, no real processes)."""

import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "clients"))

import tray_core

_EM_DASH = "\u2014"
_EN_DASH = "\u2013"


def _write_config(home, **values):
    """Write a config file under home/.usage-tracker/config."""
    config_dir = Path(home) / ".usage-tracker"
    config_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    (config_dir / "config").write_text(body)


class _FakeResponse:
    def __init__(self, body=b"{}", code=200):
        self._body = body if isinstance(body, bytes) else body.encode()
        self._code = code

    def read(self):
        return self._body

    def getcode(self):
        return self._code


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self._poll = None
        self.terminated = False
        self.killed = False
        self.wait_count = 0

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_count += 1
        if self.wait_count == 1:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self._poll = 0
        return 0


# --- api_base ---

def test_api_base_port_from_config(tmp_path):
    _write_config(tmp_path, USAGE_TRACKER_PORT="9999")
    assert tray_core.api_base(tmp_path) == "http://127.0.0.1:9999"


def test_api_base_default_port(tmp_path):
    assert tray_core.api_base(tmp_path) == "http://127.0.0.1:8000"


def test_api_base_invalid_port(tmp_path):
    _write_config(tmp_path, USAGE_TRACKER_PORT="not-a-number")
    assert tray_core.api_base(tmp_path) == "http://127.0.0.1:8000"


# --- read_secret ---

def test_read_secret_env_precedence(tmp_path):
    _write_config(tmp_path, USAGE_TRACKER_SECRET="config-secret")
    assert tray_core.read_secret(tmp_path, {"USAGE_TRACKER_SECRET": "env-secret"}) == "env-secret"


def test_read_secret_from_config(tmp_path):
    _write_config(tmp_path, USAGE_TRACKER_SECRET="config-secret")
    assert tray_core.read_secret(tmp_path, {}) == "config-secret"


def test_read_secret_empty(tmp_path):
    assert tray_core.read_secret(tmp_path, {}) == ""


def test_read_secret_default_environ(tmp_path, monkeypatch):
    _write_config(tmp_path, USAGE_TRACKER_SECRET="config-secret")
    monkeypatch.setenv("USAGE_TRACKER_SECRET", "env-secret")
    assert tray_core.read_secret(tmp_path) == "env-secret"
    monkeypatch.delenv("USAGE_TRACKER_SECRET", raising=False)
    assert tray_core.read_secret(tmp_path) == "config-secret"


# --- fetch_status ---

def test_fetch_status_success():
    stats = {"claude_quota": {"session_used_pct": 50, "weekly_used_pct": 30}}
    opener = lambda req, timeout=None: _FakeResponse(json.dumps(stats).encode(), 200)
    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": True, "stats": stats, "detail": "connected"}


def test_fetch_status_request_url_header_timeout():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return _FakeResponse(b"{}", 200)

    tray_core.fetch_status("http://127.0.0.1:8000", "my-secret", opener=opener)
    assert seen["url"] == "http://127.0.0.1:8000/stats"
    assert seen["auth"] == "Bearer my-secret"
    assert seen["timeout"] == 4.0


def test_fetch_status_401():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError("http://example.com", 401, "Unauthorized", {}, io.BytesIO(b""))

    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "The local API rejected the saved secret."}


def test_fetch_status_500():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError("http://example.com", 500, "Server Error", {}, io.BytesIO(b""))

    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "The local API answered HTTP 500."}


def test_fetch_status_urlerror():
    def opener(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "Starting the local backend..."}


def test_fetch_status_oserror():
    def opener(req, timeout=None):
        raise OSError("boom")

    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "Starting the local backend..."}


def test_fetch_status_timeout_error():
    def opener(req, timeout=None):
        raise TimeoutError("timed out")

    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "Starting the local backend..."}


def test_fetch_status_bad_json():
    opener = lambda req, timeout=None: _FakeResponse(b"not json", 200)
    result = tray_core.fetch_status("http://127.0.0.1:8000", "secret", opener=opener)
    assert result == {"connected": False, "stats": None, "detail": "Starting the local backend..."}


# --- summary_lines ---

def test_summary_lines_not_connected():
    status = {"connected": False, "stats": None, "detail": "Starting the local backend..."}
    assert tray_core.summary_lines(status) == ["Starting the local backend..."]


def test_summary_lines_none_pct():
    status = {"connected": True, "stats": {"claude_quota": {"session_used_pct": None, "weekly_used_pct": None}}, "detail": "connected"}
    assert tray_core.summary_lines(status) == ["Claude: session n/a, week n/a", "Codex: session n/a, week n/a"]


def test_summary_lines_str_pct():
    status = {"connected": True, "stats": {"claude_quota": {"session_used_pct": "abc", "weekly_used_pct": "xyz"}}, "detail": "connected"}
    assert tray_core.summary_lines(status) == ["Claude: session n/a, week n/a", "Codex: session n/a, week n/a"]


def test_summary_lines_number_pct():
    status = {
        "connected": True,
        "stats": {
            "claude_quota": {"session_used_pct": 42.7, "weekly_used_pct": 33.3},
            "codex_quota": {"session_used_pct": 10, "weekly_used_pct": 0},
        },
        "detail": "connected",
    }
    assert tray_core.summary_lines(status) == ["Claude: session 43%, week 33%", "Codex: session 10%, week 0%"]


def test_summary_lines_missing_provider():
    status = {"connected": True, "stats": {}, "detail": "connected"}
    assert tray_core.summary_lines(status) == ["Claude: session n/a, week n/a", "Codex: session n/a, week n/a"]


# --- tooltip ---

def test_tooltip():
    status = {"connected": False, "stats": None, "detail": "Starting the local backend..."}
    assert tray_core.tooltip(status) == "ai-cur desktop client: Starting the local backend..."


def test_tooltip_truncation():
    status = {"connected": False, "stats": None, "detail": "x" * 200}
    assert len(tray_core.tooltip(status)) == 127


# --- menu_spec ---

def test_menu_spec_connected():
    status = {
        "connected": True,
        "stats": {
            "claude_quota": {"session_used_pct": 50, "weekly_used_pct": 30},
            "codex_quota": {"session_used_pct": 10, "weekly_used_pct": 5},
        },
        "detail": "connected",
    }
    assert tray_core.menu_spec(status, autostart_enabled=True) == [
        {"kind": "label", "label": "Claude: session 50%, week 30%"},
        {"kind": "label", "label": "Codex: session 10%, week 5%"},
        {"kind": "separator"},
        {"kind": "action", "action": "refresh", "label": "Refresh"},
        {"kind": "action", "action": "open_logs", "label": "Open logs folder"},
        {"kind": "toggle", "action": "autostart", "label": "Start at login", "checked": True},
        {"kind": "separator"},
        {"kind": "action", "action": "quit", "label": "Quit ai-cur desktop client"},
    ]


def test_menu_spec_not_connected():
    status = {"connected": False, "stats": None, "detail": "Starting the local backend..."}
    assert tray_core.menu_spec(status, autostart_enabled=False) == [
        {"kind": "label", "label": "Starting the local backend..."},
        {"kind": "separator"},
        {"kind": "action", "action": "refresh", "label": "Refresh"},
        {"kind": "action", "action": "open_logs", "label": "Open logs folder"},
        {"kind": "toggle", "action": "autostart", "label": "Start at login", "checked": False},
        {"kind": "separator"},
        {"kind": "action", "action": "quit", "label": "Quit ai-cur desktop client"},
    ]


# --- logs_dir ---

def test_logs_dir(tmp_path):
    assert tray_core.logs_dir(tmp_path) == tmp_path / ".usage-tracker" / "logs"


def test_logs_dir_default():
    assert tray_core.logs_dir() == Path.home() / ".usage-tracker" / "logs"


# --- backend_executable ---

def test_backend_executable():
    app_dir = Path("/some/app")
    name = "aicur-backend.exe" if os.name == "nt" else "aicur-backend"
    assert tray_core.backend_executable(app_dir) == app_dir / "backend" / name


# --- BackendProcess ---

def test_backend_start_missing_exe(tmp_path):
    exe = tmp_path / "nonexistent"
    bp = tray_core.BackendProcess(exe, popen=_FakePopen)
    assert bp.start() is False
    assert bp.proc is None


def test_backend_start_command(tmp_path):
    exe = tmp_path / "aicur-backend"
    exe.write_text("")
    captured = []
