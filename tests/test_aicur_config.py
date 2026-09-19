"""Tests for packaging/backend/aicur_config.py (stdlib only)."""

import os
import sys
from pathlib import Path

import pytest

# Import the module by inserting its directory onto sys.path, not via a package
# import, so the test does not depend on packaging/backend being a package.
_BACKEND_DIR = Path(__file__).resolve().parent.parent / "packaging" / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import aicur_config  # noqa: E402


def test_config_path(tmp_path):
    assert aicur_config.config_path(tmp_path) == tmp_path / ".usage-tracker" / "config"
    assert aicur_config.config_path() == Path.home() / ".usage-tracker" / "config"


def test_read_config_missing(tmp_path):
    assert aicur_config.read_config(tmp_path / "missing-config") == {}


def test_read_config_parses_and_ignores(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "FOO=bar\n"
        "# a comment\n"
        "\n"
        "  BAZ  =  qux  \n"
        "NOEQUALS\n"
        "=emptykey\n"
        "EMPTY=\n"
    )
    result = aicur_config.read_config(path)
    assert result == {"FOO": "bar", "BAZ": "qux", "EMPTY": ""}


def test_ensure_config_generates_secret(tmp_path):
    result = aicur_config.ensure_config(tmp_path)
    assert result["USAGE_TRACKER_SECRET"]
    assert (tmp_path / ".usage-tracker" / "config").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_ensure_config_generation_modes(tmp_path):
    aicur_config.ensure_config(tmp_path)
    tracker = tmp_path / ".usage-tracker"
    assert (tracker.stat().st_mode & 0o777) == 0o700
    assert ((tracker / "config").stat().st_mode & 0o777) == 0o600


def test_ensure_config_idempotent_secret(tmp_path):
    first = aicur_config.ensure_config(tmp_path)
    second = aicur_config.ensure_config(tmp_path)
    assert first["USAGE_TRACKER_SECRET"] == second["USAGE_TRACKER_SECRET"]
    assert first["USAGE_TRACKER_SECRET"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_ensure_config_tightens_loose_mode(tmp_path):
    tracker = tmp_path / ".usage-tracker"
    tracker.mkdir(mode=0o700)
    path = tracker / "config"
    path.write_text("USAGE_TRACKER_SECRET=keepme\n")
    os.chmod(path, 0o644)
    result = aicur_config.ensure_config(tmp_path)
    assert result["USAGE_TRACKER_SECRET"] == "keepme"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text() == "USAGE_TRACKER_SECRET=keepme\n"


def test_ensure_config_preserves_other_keys(tmp_path):
    tracker = tmp_path / ".usage-tracker"
    tracker.mkdir(mode=0o700)
    path = tracker / "config"
    path.write_text("FOO=bar\nBAZ=qux\n")
    result = aicur_config.ensure_config(tmp_path)
    assert result["FOO"] == "bar"
    assert result["BAZ"] == "qux"
    assert result["USAGE_TRACKER_SECRET"]
    reread = aicur_config.read_config(path)
    assert reread["FOO"] == "bar"
    assert reread["BAZ"] == "qux"
    assert reread["USAGE_TRACKER_SECRET"] == result["USAGE_TRACKER_SECRET"]


def test_child_environment_values(tmp_path):
    cfg = aicur_config.ensure_config(tmp_path)
    env = aicur_config.child_environment(tmp_path, base={"EXISTING": "x"}, port=8000)
    assert env["EXISTING"] == "x"
    assert env["USAGE_TRACKER_SECRET"] == cfg["USAGE_TRACKER_SECRET"]
    assert env["USAGE_TRACKER_DB"] == str(tmp_path / ".usage-tracker" / "claude_usage.db")
    assert env["USAGE_TRACKER_ENV_FILE"] == str(tmp_path / ".usage-tracker" / "env")
    assert env["USAGE_TRACKER_API_URL"] == "http://127.0.0.1:8000/cc/report"
    assert env["USAGE_TRACKER_PORT"] == "8000"


def test_child_environment_port_from_config(tmp_path):
    tracker = tmp_path / ".usage-tracker"
    tracker.mkdir(mode=0o700)
    (tracker / "config").write_text("USAGE_TRACKER_SECRET=abc123\nUSAGE_TRACKER_PORT=9000\n")
    env = aicur_config.child_environment(tmp_path, base={}, port=8000)
    assert env["USAGE_TRACKER_PORT"] == "9000"
    assert env["USAGE_TRACKER_API_URL"] == "http://127.0.0.1:9000/cc/report"
    assert env["USAGE_TRACKER_SECRET"] == "abc123"
