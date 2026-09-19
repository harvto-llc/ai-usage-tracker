"""The one process-liveness probe for the whole package.

Never call os.kill(pid, 0) anywhere else. On POSIX signal 0 is a harmless existence check;
on Windows CPython implements os.kill(pid, sig) for any sig other than CTRL_C_EVENT and
CTRL_BREAK_EVENT as TerminateProcess, so the "probe" kills the process it asks about.
tests/test_process_liveness.py enforces, from the source tree, that the POSIX branch below is
the only os.kill(..., 0) in src/, clients/ and packaging/.
"""

from __future__ import annotations

import os
import sys

_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def _kernel32():  # pragma: no cover - Windows only; tests substitute a fake
    import ctypes

    return ctypes.windll.kernel32


def pid_alive(pid: object) -> bool:
    """True if a process with this pid exists. Never signals or terminates it."""
    try:
        normalized = int(pid)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    if normalized <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, normalized)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(normalized, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
