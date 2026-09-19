"""Env overrides a packaged install uses to keep data outside its read-only bundle.

Each module reads its override at import time, so every case runs in a fresh interpreter.
Unset, the defaults must be exactly what they were before the overrides existed.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = {
    "db": "from src import database; print(database.DB)",
    "env_file": "import os; os.environ.setdefault('USAGE_TRACKER_SECRET', 'x'); "
                "from src import api; print(api._sentinel_env_file())",
    "api_url": "from src import collector; print(collector.API_URL)",
}

OVERRIDES = ("USAGE_TRACKER_DB", "USAGE_TRACKER_ENV_FILE", "USAGE_TRACKER_API_URL")


def probe(name: str, **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    clean.update(env)
    result = subprocess.run(
        [sys.executable, "-c", PROBE[name]],
        cwd=ROOT,
        env=clean,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def test_defaults_unchanged_when_unset():
    assert probe("db") == str(ROOT / "claude_usage.db")
    assert probe("env_file") == str(ROOT / ".env")
    assert probe("api_url") == "http://localhost:8000/cc/report"


def test_overrides_redirect(tmp_path):
    db = tmp_path / "data" / "claude_usage.db"
    env_file = tmp_path / "env"
    assert probe("db", USAGE_TRACKER_DB=str(db)) == str(db)
    assert probe("env_file", USAGE_TRACKER_ENV_FILE=str(env_file)) == str(env_file)
    assert probe("api_url", USAGE_TRACKER_API_URL="http://127.0.0.1:18765/cc/report") == (
        "http://127.0.0.1:18765/cc/report"
    )
