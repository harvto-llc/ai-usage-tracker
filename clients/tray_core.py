"""Platform-neutral tray app core (stdlib only).

Shared by the Windows and Linux tray frontends. Talks to the local Usage
Tracker API, formats status text, and manages the backend child process.
No third-party imports so it stays safe to import before dependencies are
installed.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

if not getattr(sys, "frozen", False):
    _BACKEND_DIR = str(Path(__file__).resolve().parent.parent / "packaging" / "backend")
    if _BACKEND_DIR not in sys.path:
        sys.path.insert(0, _BACKEND_DIR)

import aicur_config


def api_base(home=None) -> str:
    """Return the local API base URL with the configured port."""
    try:
        port = int(aicur_config.read_config(aicur_config.config_path(home)).get("USAGE_TRACKER_PORT") or 8000)
    except ValueError:
        port = 8000
    return f"http://127.0.0.1:{port}"


def read_secret(home=None, environ=None) -> str:
    """Return the API secret from the environment or config."""
    if environ is None:
        environ = os.environ
    secret = environ.get("USAGE_TRACKER_SECRET", "")
    if secret:
        return secret
    return aicur_config.read_config(aicur_config.config_path(home)).get("USAGE_TRACKER_SECRET", "")


def fetch_status(base, secret, timeout=4.0, opener=None) -> dict:
    """Query the local API and return a status dict."""
    request = urllib.request.Request(base + "/stats")
    request.add_header("Authorization", f"Bearer {secret}")
    if opener is None:
        opener = urllib.request.urlopen
    try:
        response = opener(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"connected": False, "stats": None, "detail": "The local API rejected the saved secret."}
        return {"connected": False, "stats": None, "detail": f"The local API answered HTTP {exc.code}."}
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return {"connected": False, "stats": None, "detail": "Starting the local backend..."}
    try:
        body = response.read()
        code = response.getcode()
        stats = json.loads(body)
    except (ValueError, OSError):
        return {"connected": False, "stats": None, "detail": "Starting the local backend..."}
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()
    if code != 200:
        return {"connected": False, "stats": None, "detail": f"The local API answered HTTP {code}."}
    return {"connected": True, "stats": stats, "detail": "connected"}


def _format_pct(value) -> str:
    """Format a percentage value, returning 'n/a' for None or non-numbers."""
    if value is None:
        return "n/a"
    try:
        return f"{round(value)}%"
    except (TypeError, ValueError):
        return "n/a"


def summary_lines(status) -> list[str]:
    """Return human-readable status lines for the tray."""
    if not status.get("connected"):
        return [status["detail"]]
    stats = status.get("stats") or {}
    lines = []
    for provider, label in (("claude_quota", "Claude"), ("codex_quota", "Codex")):
        q = stats.get(provider) or {}
        s = _format_pct(q.get("session_used_pct"))
        w = _format_pct(q.get("weekly_used_pct"))
        lines.append(f"{label}: session {s}, week {w}")
    return lines


def tooltip(status) -> str:
    """Return a short tooltip string truncated to 127 characters."""
    text = "ai-cur desktop client: " + summary_lines(status)[0]
    return text[:127]


def menu_spec(status, autostart_enabled: bool) -> list[dict]:
    """Return the menu item specification for the tray."""
    items = [{"kind": "label", "label": line} for line in summary_lines(status)]
    items.append({"kind": "separator"})
    items.append({"kind": "action", "action": "refresh", "label": "Refresh"})
    items.append({"kind": "action", "action": "open_logs", "label": "Open logs folder"})
    items.append({"kind": "toggle", "action": "autostart", "label": "Start at login", "checked": bool(autostart_enabled)})
    items.append({"kind": "separator"})
    items.append({"kind": "action", "action": "quit", "label": "Quit ai-cur desktop client"})
    return items


def logs_dir(home=None) -> Path:
    """Return the logs directory under the Usage Tracker config dir."""
    return Path(home or Path.home()) / ".usage-tracker" / "logs"


def backend_executable(app_dir: Path) -> Path:
    """Return the backend executable path for the current platform."""
    name = "aicur-backend.exe" if os.name == "nt" else "aicur-backend"
    return Path(app_dir) / "backend" / name


class BackendProcess:
    """Manages the local backend child process lifecycle."""

    def __init__(self, executable: Path, popen=subprocess.Popen):
        self.executable = Path(executable)
        self._popen = popen
        self.proc = None

    def start(self) -> bool:
        """Start the backend if the executable exists and it is not already running."""
        if self.running():
            return True
        if not self.executable.is_file():
            return False
        cmd = [str(self.executable), "supervise", "--parent-pid", str(os.getpid())]
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        self.proc = self._popen(cmd, **kwargs)
        return True

    def running(self) -> bool:
        """Return True if the backend process is still running."""
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = 7.0):
        """Terminate the backend, escalating to kill if needed. Never raises OSError."""
        try:
            if self.running():
                self.proc.terminate()
                self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
                self.proc.wait(timeout=grace)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except OSError:
            pass
        self.proc = None
