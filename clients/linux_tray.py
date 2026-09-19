"""ai-cur desktop client for Linux: an AppIndicator tray over the backend's systemd user unit.

The backend runs as the systemd user service `aicur-backend.service` (installed by the .deb
or by the AppImage on first run). This tray shows the Claude and Codex quota, starts the
service if it is not running, and toggles its own XDG autostart entry.

    ai-cur-desktop            run the tray
    ai-cur-desktop --check    print what the tray sees, as JSON, and exit

Known GNOME defect, stated rather than hidden: stock GNOME Shell has no tray. Without the
AppIndicator extension (package gnome-shell-extension-appindicator; preinstalled and enabled on
Ubuntu) the indicator is created and never shown. The tray checks for a StatusNotifier host
at start, and when there is none it says so in a plain dialog (once) and exits 3 instead of
running invisibly. The backend keeps running either way.

Ported from Codex's unmerged tracker work (clients/linux_tray.py in
tracker-linux-workingdirectory-20260913): the binding fallback (Ayatana first, then the
legacy AppIndicator3), the StatusNotifierWatcher host probe, the refusal to run invisibly,
and the delayed republish of the menu and label. The menu content is new.

Runs on the system python3 with python3-gi; nothing here is frozen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tray_core  # noqa: E402

APP_ID = "ai-cur-desktop"
APP_TITLE = "ai-cur desktop client"
SERVICE = "aicur-backend.service"
AUTOSTART_NAME = "ai-cur-desktop.desktop"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
REFRESH_SECONDS = 30
REPUBLISH_DELAYS_SECONDS = (1.0, 3.0)

NO_TRAY_HOST_MESSAGE = (
    "ai-cur desktop client is running, but this desktop has no tray area to show its icon.\n\n"
    "On GNOME, install and enable the AppIndicator extension "
    "(package gnome-shell-extension-appindicator), then log out and back in.\n\n"
    "The backend keeps collecting in the meantime."
)
MISSING_BINDINGS_MESSAGE = (
    "The tray needs the AppIndicator bindings. Install gir1.2-ayatanaappindicator3-0.1 "
    "and python3-gi."
)


# ── Platform-neutral helpers (tested everywhere) ─────────────────────────────

def config_home(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def user_autostart_path(environ=None) -> Path:
    return config_home(environ) / "autostart" / AUTOSTART_NAME


def autostart_enabled(environ=None, system_dirs=None) -> bool:
    """XDG rule: a user entry overrides the system one; Hidden=true disables it."""
    user = user_autostart_path(environ)
    if user.is_file():
        return not _desktop_hidden(user)
    dirs = system_dirs if system_dirs is not None else _xdg_config_dirs(environ)
    return any((Path(d) / "autostart" / AUTOSTART_NAME).is_file() for d in dirs)


def set_autostart(enabled: bool, exec_line: str, environ=None) -> Path:
    """Write the per-user entry: a real entry to enable, Hidden=true to disable."""
    path = user_autostart_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry(exec_line, hidden=not enabled), encoding="utf-8")
    return path


def autostart_exec(environ=None) -> str:
    """What the autostart entry runs: the AppImage itself, or the .deb's launcher."""
    environ = os.environ if environ is None else environ
    appimage = environ.get("APPIMAGE")
    return f'"{appimage}"' if appimage else APP_ID


def desktop_entry(exec_line: str, hidden: bool = False) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_TITLE}",
        "Comment=Claude and Codex quota and activity, collected locally",
        f"Exec={exec_line}",
        "Icon=utilities-system-monitor",
        "Terminal=false",
        "Categories=Utility;",
        "X-GNOME-Autostart-enabled=true",
    ]
    if hidden:
        lines.append("Hidden=true")
    return "\n".join(lines) + "\n"


def _desktop_hidden(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.strip().lower() == "hidden=true" for line in text.splitlines())


def _xdg_config_dirs(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    return [d for d in (environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]


def ensure_backend(run=subprocess.run, which=shutil.which) -> str:
    """Start the systemd user service if it is not active. Returns what happened."""
    if which("systemctl") is None:
        return "no systemctl: start the backend with `aicur-backend supervise`"
    active = run(["systemctl", "--user", "is-active", "--quiet", SERVICE], check=False)
    if active.returncode == 0:
        return "active"
    started = run(["systemctl", "--user", "start", SERVICE], check=False)
    return "started" if started.returncode == 0 else f"could not start {SERVICE}"


def warned_marker(home=None) -> Path:
    return Path(home or Path.home()) / ".usage-tracker" / "no-tray-host-warned"


def should_warn_no_host(home=None) -> bool:
    """Show the no-tray-host dialog once per user, not at every login."""
    return not warned_marker(home).exists()


def mark_warned(home=None) -> None:
    marker = warned_marker(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("shown\n", encoding="utf-8")


def visibility_report(bindings_problem: str | None, host_present: bool) -> dict:
    if bindings_problem is not None:
        return {"can_be_visible": False, "reason": bindings_problem}
    if not host_present:
        return {"can_be_visible": False, "reason": NO_TRAY_HOST_MESSAGE}
    return {"can_be_visible": True, "reason": "bindings present and a tray host is listening"}


# ── GI layer ─────────────────────────────────────────────────────────────────

def load_indicator_bindings():
    """(Gtk, AppIndicator module) or (None, reason). Ayatana first, legacy second."""
    try:
        import gi
    except ImportError:
        return None, "PyGObject is missing. Install python3-gi."
    for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(namespace, "0.1")
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk  # noqa: PLC0415

            module = __import__("gi.repository", fromlist=[namespace])
            return (Gtk, getattr(module, namespace)), None
        except (ValueError, ImportError):
            continue
    return None, MISSING_BINDINGS_MESSAGE


def tray_host_present() -> bool:
    """Does anything own the StatusNotifierWatcher name on the session bus?"""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib  # noqa: PLC0415

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (WATCHER_NAME,)), GLib.VariantType("(b)"),
            0, 2000, None,
        )
        return bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 - no bus, no GI: no host
        return False


def show_dialog(gtk, message: str) -> None:  # pragma: no cover - needs a display
    dialog = gtk.MessageDialog(message_type=gtk.MessageType.INFO, buttons=gtk.ButtonsType.OK,
                               text=APP_TITLE)
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


def run_indicator(gtk, appindicator) -> int:  # pragma: no cover - needs a display
    from gi.repository import GLib  # noqa: PLC0415

    base = tray_core.api_base()
    state = {"status": tray_core.fetch_status(base, tray_core.read_secret())}
    indicator = appindicator.Indicator.new(
        APP_ID, "utilities-system-monitor", appindicator.IndicatorCategory.APPLICATION_STATUS)
    indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
    indicator.set_title(APP_TITLE)

    def build_menu():
        menu = gtk.Menu()
        for item in tray_core.menu_spec(state["status"], autostart_enabled()):
            if item["kind"] == "separator":
                menu.append(gtk.SeparatorMenuItem())
                continue
            if item["kind"] == "toggle":
                entry = gtk.CheckMenuItem(label=item["label"])
                entry.set_active(item["checked"])
                entry.connect("toggled", lambda w: (set_autostart(w.get_active(), autostart_exec()), refresh()))
            else:
                entry = gtk.MenuItem(label=item["label"])
                if item["kind"] == "label":
                    entry.set_sensitive(False)
                elif item["action"] == "refresh":
                    entry.connect("activate", lambda _w: refresh())
                elif item["action"] == "open_logs":
                    entry.connect("activate", lambda _w: open_logs())
                elif item["action"] == "quit":
                    entry.connect("activate", lambda _w: gtk.main_quit())
            menu.append(entry)
        menu.show_all()
        return menu

    def publish():
        indicator.set_menu(build_menu())
        indicator.set_label(tray_core.summary_lines(state["status"])[0][:48], APP_ID)
        return False

    def refresh():
        state["status"] = tray_core.fetch_status(base, tray_core.read_secret())
        publish()
        return True

    def open_logs():
        logs = tray_core.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(logs)], stdin=subprocess.DEVNULL,  # noqa: S603, S607
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    publish()
    # Properties set before the shell attaches to the new item are not re-sent, so the label
    # is missing until something republishes. Bounded retry, each attempt idempotent.
    for delay in REPUBLISH_DELAYS_SECONDS:
        GLib.timeout_add(int(delay * 1000), publish)
    GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)
    gtk.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    bindings, problem = load_indicator_bindings()
    host = tray_host_present()
    visibility = visibility_report(problem, host)
    if "--check" in argv:
        status = tray_core.fetch_status(tray_core.api_base(), tray_core.read_secret())
        print(json.dumps({
            "api_base": tray_core.api_base(),
            "connected": status["connected"],
            "summary": tray_core.summary_lines(status),
            "bindings_available": problem is None,
            "tray_host_present": host,
            "can_be_visible": visibility["can_be_visible"],
            "reason": visibility["reason"],
            "autostart_enabled": autostart_enabled(),
        }, indent=2))
        return 0

    print(f"backend: {ensure_backend()}", file=sys.stderr)
    if bindings is None:
        print(problem, file=sys.stderr)
        return 2
    gtk, appindicator = bindings
    if not visibility["can_be_visible"]:
        print(visibility["reason"], file=sys.stderr)
        if should_warn_no_host() and os.environ.get("AICUR_SMOKE") != "1":
            try:
                show_dialog(gtk, NO_TRAY_HOST_MESSAGE)
            finally:
                mark_warned()
        return 3
    return run_indicator(gtk, appindicator)


if __name__ == "__main__":
    raise SystemExit(main())
