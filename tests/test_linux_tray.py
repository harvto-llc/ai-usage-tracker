"""Platform-neutral half of clients/linux_tray.py. Runs on every OS; GI is not needed.

The AppIndicator layer is exercised by scripts/smoke_linux.sh on ubuntu-22.04 in CI.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CLIENTS = Path(__file__).resolve().parent.parent / "clients"
if str(_CLIENTS) not in sys.path:
    sys.path.insert(0, str(_CLIENTS))

import linux_tray  # noqa: E402


def env(tmp_path, **extra):
    values = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CONFIG_DIRS": str(tmp_path / "xdg")}
    values.update(extra)
    return values


def test_autostart_follows_the_xdg_override_rules(tmp_path):
    e = env(tmp_path)
    assert not linux_tray.autostart_enabled(e)
    system = tmp_path / "xdg" / "autostart"
    system.mkdir(parents=True)
    (system / linux_tray.AUTOSTART_NAME).write_text(linux_tray.desktop_entry("ai-cur-desktop"))
    assert linux_tray.autostart_enabled(e)  # the .deb's /etc/xdg entry
    linux_tray.set_autostart(False, "ai-cur-desktop", e)
    assert not linux_tray.autostart_enabled(e)  # a user Hidden=true entry wins
    assert "Hidden=true" in linux_tray.user_autostart_path(e).read_text()
    linux_tray.set_autostart(True, "ai-cur-desktop", e)
    assert linux_tray.autostart_enabled(e)


def test_autostart_exec_uses_the_appimage_when_there_is_one():
    assert linux_tray.autostart_exec({}) == "ai-cur-desktop"
    assert linux_tray.autostart_exec({"APPIMAGE": "/home/u/Apps/ai cur.AppImage"}) == '"/home/u/Apps/ai cur.AppImage"'


def test_desktop_entry_is_well_formed():
    entry = linux_tray.desktop_entry("ai-cur-desktop")
    lines = entry.splitlines()
    assert lines[0] == "[Desktop Entry]"
    keys = dict(line.split("=", 1) for line in lines[1:])
    assert keys["Type"] == "Application"
    assert keys["Exec"] == "ai-cur-desktop"
    assert keys["Name"] == "ai-cur desktop client"
    assert "Hidden" not in keys
    assert "Hidden=true" in linux_tray.desktop_entry("x", hidden=True)


def run_recorder(results):
    calls = []

    def run(argv, check=False):
        calls.append(argv)
        return SimpleNamespace(returncode=results.pop(0))

    return run, calls


def test_ensure_backend_leaves_an_active_service_alone():
    run, calls = run_recorder([0])
    assert linux_tray.ensure_backend(run=run, which=lambda name: "/bin/systemctl") == "active"
    assert calls == [["systemctl", "--user", "is-active", "--quiet", "aicur-backend.service"]]


def test_ensure_backend_starts_an_inactive_service():
    run, calls = run_recorder([3, 0])
    assert linux_tray.ensure_backend(run=run, which=lambda name: "/bin/systemctl") == "started"
    assert calls[1] == ["systemctl", "--user", "start", "aicur-backend.service"]


def test_ensure_backend_reports_failure_and_missing_systemctl():
    run, _ = run_recorder([3, 1])
    assert "could not start" in linux_tray.ensure_backend(run=run, which=lambda name: "/bin/systemctl")
    assert "no systemctl" in linux_tray.ensure_backend(run=run, which=lambda name: None)


def test_no_tray_host_is_reported_plainly():
    report = linux_tray.visibility_report(None, host_present=False)
    assert not report["can_be_visible"]
    assert "AppIndicator extension" in report["reason"]
    assert "gnome-shell-extension-appindicator" in report["reason"]
    assert linux_tray.visibility_report("missing bindings", True) == {
        "can_be_visible": False, "reason": "missing bindings"}
    assert linux_tray.visibility_report(None, True)["can_be_visible"]


def test_the_no_host_dialog_is_shown_once(tmp_path):
    assert linux_tray.should_warn_no_host(tmp_path)
    linux_tray.mark_warned(tmp_path)
    assert not linux_tray.should_warn_no_host(tmp_path)


@pytest.mark.parametrize("text", [linux_tray.NO_TRAY_HOST_MESSAGE, linux_tray.MISSING_BINDINGS_MESSAGE])
def test_user_facing_messages_have_no_em_or_en_dash(text):
    assert "—" not in text and "–" not in text
