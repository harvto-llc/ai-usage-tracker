"""ai-cur desktop client for Windows: a notification-area icon that owns the backend.

On start it launches `backend\\aicur-backend.exe supervise --parent-pid <this pid>` from the
install folder, shows the Claude and Codex quota in its menu and tooltip, and stops the
backend on Quit. If the tray is killed instead, the supervisor notices its parent is gone and
stops the API and the collector itself.

    aicur-tray.exe            run (one instance per user; a second start exits at once)
    aicur-tray.exe --quit     ask the running tray to quit cleanly (used by the installer
                              and the CI smoke test)
    aicur-tray.exe --check    print what the tray would do, as JSON, and exit

The Win32 layer (struct layouts, prototypes, the NOTIFYICON_VERSION_4 negotiation, the
TaskbarCreated re-add and the SetForegroundWindow/WM_NULL menu idiom) is ported from Codex's
unmerged tracker work (clients/windows_tray.py in tracker-linux-workingdirectory-20260913).
The menu content is new: that tray drove a web dashboard this repository does not have.

AICUR_SMOKE=1: when no notification area exists (a CI session), keep running without an icon
instead of refusing, so the backend lifecycle can still be tested.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(HERE))

import tray_core  # noqa: E402

APP_TITLE = "ai-cur desktop client"
WINDOW_CLASS = "AicurDesktopTrayWindow"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_TITLE
REFRESH_MS = 30_000
REFRESH_TIMER_ID = 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NOTIFYICON_VERSION_4 = 4
TRAY_ICON_ID = 1

WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
IDI_APPLICATION = 32512

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_CHECKED = 0x00000008
MF_SEPARATOR = 0x00000800
FIRST_COMMAND_ID = 100


# ── Platform-neutral helpers (tested on every OS) ────────────────────────────

def app_dir() -> Path:
    """Install folder: the tray exe sits at its root, the backend under backend\\."""
    override = os.environ.get("AICUR_APP_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return HERE.parent


def tray_command() -> str:
    """The Run-key command line that starts this tray at login."""
    return f'"{Path(sys.executable).resolve()}"'


def autostart_enabled(reg=None) -> bool:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, RUN_VALUE)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool, command: str, reg=None) -> None:
    reg = reg or _winreg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        if enabled:
            reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, command)
        else:
            try:
                reg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def menu_commands(spec: list[dict]) -> list[dict]:
    """Number the actionable entries; labels and separators get no id."""
    commands = []
    next_id = FIRST_COMMAND_ID
    for entry in spec:
        item = dict(entry)
        if entry["kind"] in ("action", "toggle"):
            item["command_id"] = next_id
            next_id += 1
        commands.append(item)
    return commands


def command_flags(entry: dict) -> int:
    if entry["kind"] == "label":
        return MF_STRING | MF_GRAYED
    if entry["kind"] == "toggle" and entry.get("checked"):
        return MF_STRING | MF_CHECKED
    return MF_STRING


def resolve_command(commands: list[dict], command_id: int) -> dict | None:
    if not command_id:
        return None
    for entry in commands:
        if entry.get("command_id") == command_id:
            return entry
    return None


def low_word(value: int) -> int:
    return value & 0xFFFF


def high_word(value: int) -> int:
    return (value >> 16) & 0xFFFF


def signed_word(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


def _winreg():  # pragma: no cover - Windows only
    import winreg

    return winreg


# ── Win32 layer (Windows only; exercised by the CI smoke test) ────────────────

_WIN32 = None


def win32():  # pragma: no cover - requires Windows
    """Prototype every API used, once. Unprototyped calls truncate handles on Win64."""
    global _WIN32
    if _WIN32 is not None:
        return _WIN32
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        # The full Vista-and-later layout: Shell_NotifyIcon validates cbSize against the
        # sizes it knows, so a truncated struct is an invalid call, not a smaller one.
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    proto = [
        (user32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM),
        (user32.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID],
         wintypes.HWND),
        (user32.DefWindowProcW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT),
        (user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.PostQuitMessage, [ctypes.c_int], None),
        (user32.GetMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT],
         wintypes.BOOL),
        (user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        (user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], LRESULT),
        (user32.CreatePopupMenu, [], wintypes.HMENU),
        (user32.AppendMenuW, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR],
         wintypes.BOOL),
        (user32.DestroyMenu, [wintypes.HMENU], wintypes.BOOL),
        (user32.TrackPopupMenu, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wintypes.HWND, wintypes.LPVOID], wintypes.BOOL),
        (user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
         wintypes.BOOL),
        (user32.LoadIconW, [wintypes.HINSTANCE, wintypes.LPCWSTR], wintypes.HICON),
        (user32.RegisterWindowMessageW, [wintypes.LPCWSTR], wintypes.UINT),
        (user32.FindWindowW, [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        (user32.SetTimer, [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID], ctypes.c_size_t),
        (user32.KillTimer, [wintypes.HWND, ctypes.c_size_t], wintypes.BOOL),
        (shell32.Shell_NotifyIconW, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL),
        (kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE),
    ]
    for func, argtypes, restype in proto:
        func.argtypes = argtypes
        func.restype = restype

    _WIN32 = {
        "ctypes": ctypes, "wintypes": wintypes, "user32": user32, "shell32": shell32,
        "kernel32": kernel32, "WNDPROC": WNDPROC, "NOTIFYICONDATAW": NOTIFYICONDATAW,
        "WNDCLASSW": WNDCLASSW,
    }
    return _WIN32


def find_running_tray():  # pragma: no cover - requires Windows
    return win32()["user32"].FindWindowW(WINDOW_CLASS, None)


class Tray:  # pragma: no cover - requires Windows
    def __init__(self, backend: tray_core.BackendProcess, smoke: bool) -> None:
        self.backend = backend
        self.smoke = smoke
        self.w = win32()
        self.base = tray_core.api_base()
        self.status: dict = {"connected": False, "stats": None, "detail": "Starting the local backend..."}
        self.commands: list[dict] = []
        self.icon = None
        self.hwnd = None
        self.exit_status = 0
        self.taskbar_created = self.w["user32"].RegisterWindowMessageW("TaskbarCreated")
        self._proc = self.w["WNDPROC"](self._wndproc)  # kept alive: freed WNDPROC = crash

    def refresh(self) -> None:
        self.status = tray_core.fetch_status(self.base, tray_core.read_secret())
        spec = tray_core.menu_spec(self.status, autostart_enabled())
        self.commands = menu_commands(spec)
        if self.icon is not None:
            self.icon.szTip = tray_core.tooltip(self.status)
            self.w["shell32"].Shell_NotifyIconW(NIM_MODIFY, self.w["ctypes"].byref(self.icon))

    def _notify_data(self):
        ctypes = self.w["ctypes"]
        data = self.w["NOTIFYICONDATAW"]()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = TRAY_ICON_ID
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self.w["user32"].LoadIconW(
            None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), self.w["wintypes"].LPCWSTR))
        data.szTip = tray_core.tooltip(self.status)
        return data

    def add_icon(self) -> bool:
        ctypes, shell32 = self.w["ctypes"], self.w["shell32"]
        data = self._notify_data()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            return False
        # The version is not retained across NIM_ADD, and wndproc decodes v4 packing, so a
        # failed negotiation must not be ignored.
        data.uVersion = NOTIFYICON_VERSION_4
        if not shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data)):
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            return False
        self.icon = data
        return True

    def show_menu(self, x: int, y: int) -> dict | None:
        user32 = self.w["user32"]
        menu = user32.CreatePopupMenu()
        if not menu:
            return None
        try:
            for entry in self.commands:
                if entry["kind"] == "separator":
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    user32.AppendMenuW(menu, command_flags(entry), entry.get("command_id", 0), entry["label"])
            user32.SetForegroundWindow(self.hwnd)  # or the menu never dismisses
            chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                           x, y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)  # or the next click is swallowed
        finally:
            user32.DestroyMenu(menu)
        return resolve_command(self.commands, chosen)

    def handle(self, entry: dict | None) -> None:
        if entry is None:
            return
        action = entry.get("action")
        if action == "refresh":
            self.refresh()
        elif action == "open_logs":
            logs = tray_core.logs_dir()
            logs.mkdir(parents=True, exist_ok=True)
            os.startfile(str(logs))  # noqa: S606 - opens Explorer on our own folder
        elif action == "autostart":
            set_autostart(not entry.get("checked"), tray_command())
            self.refresh()
        elif action == "quit":
            self.w["user32"].DestroyWindow(self.hwnd)

    def _wndproc(self, hwnd, message, wparam, lparam):
        user32 = self.w["user32"]
        try:
            if message == self.taskbar_created and self.taskbar_created:
                self.icon = None
                if not self.add_icon() and not self.smoke:
                    self.exit_status = 6
                    user32.DestroyWindow(hwnd)
                return 0
            if message == WM_TRAY_CALLBACK:
                if low_word(lparam) in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP):
                    self.refresh()
                    self.handle(self.show_menu(signed_word(low_word(wparam)), signed_word(high_word(wparam))))
                return 0
            if message == WM_TIMER:
                self.refresh()
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                user32.KillTimer(hwnd, REFRESH_TIMER_ID)
                user32.PostQuitMessage(self.exit_status)
                return 0
        except Exception as exc:  # noqa: BLE001 - nothing may escape a ctypes callback
            tray_core_log(f"tray callback failed: {type(exc).__name__}: {exc}")
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> int:
        ctypes, wintypes = self.w["ctypes"], self.w["wintypes"]
        user32, kernel32 = self.w["user32"], self.w["kernel32"]
        wndclass = self.w["WNDCLASSW"]()
        wndclass.lpfnWndProc = self._proc
        wndclass.lpszClassName = WINDOW_CLASS
        wndclass.hInstance = kernel32.GetModuleHandleW(None)
        self._wndclass = wndclass
        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            raise RuntimeError("Could not register the tray window class.")
        self.hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, APP_TITLE, 0, 0, 0, 0, 0,
                                           None, None, wndclass.hInstance, None)
        if not self.hwnd:
            raise RuntimeError("Could not create the tray window.")
        self.backend.start()
        self.refresh()
        if not self.add_icon():
            if not self.smoke:
                user32.DestroyWindow(self.hwnd)
                raise RuntimeError("Windows refused to add the notification-area icon.")
            tray_core_log("no notification area; AICUR_SMOKE=1, running without an icon")
        user32.SetTimer(self.hwnd, REFRESH_TIMER_ID, REFRESH_MS, None)
        message = wintypes.MSG()
        status = 0
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    status = message.wParam
                    break
                if result == -1:
                    status = 5
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self.icon is not None:
                self.w["shell32"].Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.icon))
            self.backend.stop()
        return int(status or self.exit_status)


def tray_core_log(message: str) -> None:
    """Report without a console: DETACHED/--windowed processes have sys.stderr None."""
    try:
        logs = tray_core.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "tray.log", "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass
    stream = sys.stderr
    if stream is not None:
        try:
            print(message, file=stream)
        except (OSError, ValueError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    backend = tray_core.BackendProcess(tray_core.backend_executable(app_dir()))
    if "--check" in argv:
        status = tray_core.fetch_status(tray_core.api_base(), tray_core.read_secret())
        print(json.dumps({
            "api_base": tray_core.api_base(),
            "backend_executable": str(backend.executable),
            "backend_present": backend.executable.is_file(),
            "connected": status["connected"],
            "summary": tray_core.summary_lines(status),
        }, indent=2))
        return 0
    if os.name != "nt":
        tray_core_log("windows_tray runs on Windows only")
        return 2
    existing = find_running_tray()  # pragma: no cover - Windows only below
    if "--quit" in argv:
        if not existing:
            tray_core_log("no running tray to quit")
            return 1
        win32()["user32"].PostMessageW(existing, WM_CLOSE, 0, 0)
        return 0
    if existing:
        return 0  # already running for this user
    smoke = os.environ.get("AICUR_SMOKE") == "1"
    try:
        return Tray(backend, smoke).run()
    except Exception as exc:  # noqa: BLE001
        tray_core_log(f"The tray failed to start: {type(exc).__name__}: {exc}")
        backend.stop()
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
