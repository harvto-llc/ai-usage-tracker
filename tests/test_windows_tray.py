"""Platform-neutral half of clients/windows_tray.py. Runs on every OS.

The Win32 layer (window, notify icon, message loop) is exercised by the Windows smoke test
in CI (scripts/smoke_windows.ps1), not here.
"""

import sys
from pathlib import Path

import pytest

_CLIENTS = Path(__file__).resolve().parent.parent / "clients"
if str(_CLIENTS) not in sys.path:
    sys.path.insert(0, str(_CLIENTS))

windows_tray = pytest.importorskip("windows_tray")


class FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.paths = []

    def OpenKey(self, root, path, reserved, access):
        self.paths.append((root, path))
        return FakeKey(self.values)

    def CreateKeyEx(self, root, path, reserved, access):
        self.paths.append((root, path))
        return FakeKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


def test_autostart_round_trip_uses_the_per_user_run_key():
    reg = FakeWinreg()
    assert not windows_tray.autostart_enabled(reg)
    windows_tray.set_autostart(True, '"C:\\x\\aicur-tray.exe"', reg)
    assert windows_tray.autostart_enabled(reg)
    assert reg.values[windows_tray.RUN_VALUE] == '"C:\\x\\aicur-tray.exe"'
    windows_tray.set_autostart(False, "", reg)
    assert not windows_tray.autostart_enabled(reg)
    windows_tray.set_autostart(False, "", reg)  # removing an absent value is not an error
    assert all(root == "HKCU" for root, _ in reg.paths)
    assert all(path == r"Software\Microsoft\Windows\CurrentVersion\Run" for _, path in reg.paths)


def test_run_value_name_matches_the_installer():
    iss = (Path(__file__).resolve().parent.parent / "packaging" / "windows" / "aicur-desktop.iss").read_text()
    assert f'#define AppName "{windows_tray.RUN_VALUE}"' in iss
    assert 'ValueName: "{#AppName}"' in iss


SPEC = [
    {"kind": "label", "label": "Claude: session 10%, week 20%"},
    {"kind": "separator"},
    {"kind": "action", "action": "refresh", "label": "Refresh"},
    {"kind": "toggle", "action": "autostart", "label": "Start at login", "checked": True},
    {"kind": "toggle", "action": "autostart", "label": "Start at login", "checked": False},
    {"kind": "action", "action": "quit", "label": "Quit"},
]


def test_menu_commands_number_only_actionable_entries():
    commands = windows_tray.menu_commands(SPEC)
    ids = [c.get("command_id") for c in commands]
    assert ids == [None, None, 100, 101, 102, 103]
    assert windows_tray.resolve_command(commands, 103)["action"] == "quit"
    assert windows_tray.resolve_command(commands, 0) is None  # TrackPopupMenu: dismissed
    assert windows_tray.resolve_command(commands, 999) is None


def test_command_flags():
    commands = windows_tray.menu_commands(SPEC)
    assert windows_tray.command_flags(commands[0]) & windows_tray.MF_GRAYED
    assert windows_tray.command_flags(commands[3]) & windows_tray.MF_CHECKED
    assert not windows_tray.command_flags(commands[4]) & windows_tray.MF_CHECKED
    assert windows_tray.command_flags(commands[2]) == windows_tray.MF_STRING


def test_word_helpers_decode_v4_packing():
    packed = (0xFFFE << 16) | 0x0205
    assert windows_tray.low_word(packed) == 0x0205
    assert windows_tray.high_word(packed) == 0xFFFE
    assert windows_tray.signed_word(0xFFFE) == -2  # a menu anchor left of the primary monitor
    assert windows_tray.signed_word(0x7FFF) == 0x7FFF


def test_app_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AICUR_APP_DIR", str(tmp_path))
    assert windows_tray.app_dir() == tmp_path


def test_non_windows_main_refuses(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows runs the real tray")
    assert windows_tray.main([]) == 2
