"""Local Usage Tracker configuration helpers (stdlib only).

Manages ``~/.usage-tracker/config``: the shared API secret, the small
KEY=VALUE parser used to read it, and the child-process environment derived
from it. No third-party imports, so it is safe to run before the project's
own dependencies are installed.
"""

import os
import secrets
from pathlib import Path

CONFIG_DIR = ".usage-tracker"
CONFIG_NAME = "config"
SECRET_KEY = "USAGE_TRACKER_SECRET"


def config_path(home: Path | None = None) -> Path:
    """Return the config file path ``<home>/.usage-tracker/config``.

    ``home`` defaults to :func:`pathlib.Path.home`.
    """
    base = Path.home() if home is None else Path(home)
    return base / CONFIG_DIR / CONFIG_NAME


def read_config(path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from ``path``.

    Blank lines and lines beginning with ``#`` are ignored. Whitespace around
    the key and the value is stripped. Returns an empty dict when the file is
    missing or unreadable.
    """
    path = Path(path)
    config: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        config[key] = value.strip()
    return config


def ensure_config(home: Path | None = None) -> dict[str, str]:
    """Create or repair the Usage Tracker config and return its parsed contents.

    Creates ``<home>/.usage-tracker`` with mode 0o700 if missing. When a
    non-empty ``USAGE_TRACKER_SECRET`` is already present the file content is
    kept unchanged and, on POSIX, its permissions are tightened to 0o600 when
    they are looser. Otherwise a fresh secret is generated and the file is
    rewritten atomically, preserving any other existing KEY=VALUE lines and
    appending ``USAGE_TRACKER_SECRET=<token>``. On Windows chmod is skipped.
    """
    base = Path.home() if home is None else Path(home)
    tracker_dir = base / CONFIG_DIR
    if not tracker_dir.exists():
        tracker_dir.mkdir(mode=0o700, parents=True)
        if os.name != "nt":
            os.chmod(tracker_dir, 0o700)
    path = config_path(base)
    existing = read_config(path)
    if existing.get(SECRET_KEY):
        if os.name != "nt":
            try:
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                mode = 0o600
            if mode & 0o077:
                os.chmod(path, 0o600)
        return existing
    token = secrets.token_urlsafe(32)
    try:
        raw = path.read_text()
    except OSError:
        raw = ""
    body = raw
    if body and not body.endswith("\n"):
        body += "\n"
    body += f"{SECRET_KEY}={token}\n"
    tmp_path = tracker_dir / f".{CONFIG_NAME}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, body.encode())
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return read_config(path)


def child_environment(
    home: Path | None = None,
    base: dict | None = None,
    port: int = 8000,
) -> dict[str, str]:
    """Build a child-process environment with Usage Tracker variables set.

    Returns a copy of ``base`` (default :data:`os.environ`) with the secret from
    :func:`ensure_config` plus the database, env file, API URL and port. When
    the config contains ``USAGE_TRACKER_PORT`` that value wins over ``port``.
    """
    base_home = Path.home() if home is None else Path(home)
    config = ensure_config(base_home)
    env = dict(base if base is not None else os.environ)
    configured_port = config.get("USAGE_TRACKER_PORT")
    if configured_port:
        try:
            port = int(configured_port)
        except ValueError:
            pass
    env[SECRET_KEY] = config[SECRET_KEY]
    env["USAGE_TRACKER_DB"] = str(base_home / CONFIG_DIR / "claude_usage.db")
    env["USAGE_TRACKER_ENV_FILE"] = str(base_home / CONFIG_DIR / "env")
    env["USAGE_TRACKER_API_URL"] = f"http://127.0.0.1:{port}/cc/report"
    env["USAGE_TRACKER_PORT"] = str(port)
    return env
