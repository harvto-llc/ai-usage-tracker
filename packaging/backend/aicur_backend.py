"""ai-cur desktop backend: the one executable every installer ships.

PyInstaller freezes this file (one-dir) into `aicur-backend`. The GUI shells (the macOS menu
bar app, the Windows tray, the Linux tray) only ever launch `aicur-backend supervise`; the
supervisor owns the API and the collector loop as its own children.

    aicur-backend supervise        config, then API + collector loop, restart on death
    aicur-backend api              uvicorn on 127.0.0.1 only
    aicur-backend collector-loop   one collector cycle every --interval seconds
    aicur-backend collect-once     one collector cycle (what launchd runs for source installs)

The API binds 127.0.0.1 and nothing else; there is deliberately no --host option.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, IO

FROZEN = getattr(sys, "frozen", False)
HERE = Path(__file__).resolve().parent
if not FROZEN:
    # Source checkout: make `src` importable and `aicur_config` a sibling import.
    sys.path.insert(0, str(HERE.parents[1]))
    sys.path.insert(0, str(HERE))

import aicur_config  # noqa: E402
from src.process_liveness import pid_alive  # noqa: E402,F401 - the one liveness probe

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8000
COLLECTOR_INTERVAL = 60
BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)
STABLE_AFTER_SECONDS = 60
STOP_GRACE_SECONDS = 5
# Windows: the backend is a console program started without a console; without this flag
# every child it starts would open a visible console window.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def self_command(subcommand: str, *args: str) -> list[str]:
    """argv that re-invokes this backend with another subcommand."""
    if FROZEN:
        return [sys.executable, subcommand, *args]
    return [sys.executable, str(Path(__file__).resolve()), subcommand, *args]


# ---------------------------------------------------------------------------
# Process liveness: src/process_liveness.pid_alive, the one probe (see its docstring).
# ---------------------------------------------------------------------------

def parent_watch(parent_pid: int | None) -> Callable[[], bool]:
    """Return a callable that is True while the process that launched us is still there.

    POSIX: reparenting changes getppid(), which also covers a recycled pid. Windows has no
    reparenting, so the launcher passes its pid and we check that pid directly.
    """
    if os.name != "nt":
        original = os.getppid()
        return lambda: os.getppid() == original
    if parent_pid:
        return lambda: pid_alive(parent_pid)
    return lambda: True


def start_orphan_watchdog(alive: Callable[[], bool], interval: float = 1.0) -> None:
    """Exit hard if our parent disappears (covers a SIGKILLed supervisor)."""

    def watch() -> None:
        while True:
            time.sleep(interval)
            if not alive():
                os._exit(0)

    threading.Thread(target=watch, name="orphan-watchdog", daemon=True).start()


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

@dataclass
class ChildSpec:
    name: str
    argv: list[str]


@dataclass
class ChildState:
    spec: ChildSpec
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    next_start: float = 0.0
    failures: int = 0
    restarts: int = 0
    exits: list[int] = field(default_factory=list)


class Supervisor:
    """Keep a fixed set of children running; stop all of them on request.

    Pure policy plus injected effects (popen, clock), so the restart and shutdown logic is
    unit-tested without real processes or real time.
    """

    def __init__(
        self,
        specs: list[ChildSpec],
        *,
        env: dict[str, str],
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        log_for: Callable[[str], IO | None] = lambda name: None,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        stable_after: float = STABLE_AFTER_SECONDS,
    ) -> None:
        self.children = [ChildState(spec) for spec in specs]
        self.env = env
        self.popen = popen
        self.clock = clock
        self.log_for = log_for
        self.backoff = backoff
        self.stable_after = stable_after

    def _start(self, child: ChildState, now: float) -> None:
        log = self.log_for(child.spec.name)
        child.proc = self.popen(
            child.spec.argv,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=log if log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log is not None else subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        child.started_at = now

    def step(self) -> None:
        """Start what is due, notice what died, schedule its restart."""
        now = self.clock()
        for child in self.children:
            if child.proc is None:
                if now >= child.next_start:
                    self._start(child, now)
                continue
            code = child.proc.poll()
            if code is None:
                continue
            child.exits.append(code)
            if now - child.started_at >= self.stable_after:
                child.failures = 0
            delay = self.backoff[min(child.failures, len(self.backoff) - 1)]
            child.failures += 1
            child.restarts += 1
            child.proc = None
            child.next_start = now + delay

    def running(self) -> list[subprocess.Popen]:
        return [c.proc for c in self.children if c.proc is not None and c.proc.poll() is None]

    def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        procs = self.running()
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = self.clock() + grace
        for proc in procs:
            remaining = max(0.0, deadline - self.clock())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=grace)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for child in self.children:
            child.proc = None


def _open_log(logs_dir: Path, name: str) -> IO:
    logs_dir.mkdir(parents=True, exist_ok=True)
    return open(logs_dir / f"{name}.log", "ab", buffering=0)


def run_supervise(args: argparse.Namespace) -> int:
    home = Path.home()
    env = aicur_config.child_environment(home=home, port=args.port)
    port = int(env["USAGE_TRACKER_PORT"])
    logs_dir = home / ".usage-tracker" / "logs"
    me = str(os.getpid())
    specs = [
        ChildSpec("api", self_command("api", "--port", str(port), "--parent-pid", me)),
        ChildSpec(
            "collector",
            self_command(
                "collector-loop",
                "--port", str(port),
                "--interval", str(args.interval),
                "--parent-pid", me,
            ),
        ),
    ]
    logs: dict[str, IO] = {}

    def log_for(name: str) -> IO:
        if name not in logs:
            logs[name] = _open_log(logs_dir, name)
        return logs[name]

    supervisor = Supervisor(specs, env=env, log_for=log_for)
    stopping = threading.Event()

    def request_stop(signum, frame) -> None:  # noqa: ARG001
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    parent_alive = parent_watch(args.parent_pid)
    try:
        while not stopping.is_set() and parent_alive():
            supervisor.step()
            stopping.wait(0.5)
    finally:
        supervisor.stop()
        for handle in logs.values():
            handle.close()
    return 0


# ---------------------------------------------------------------------------
# API and collector
# ---------------------------------------------------------------------------

def run_api(args: argparse.Namespace) -> int:
    start_orphan_watchdog(parent_watch(args.parent_pid))
    import uvicorn

    from src.api import app

    uvicorn.run(app, host=LOOPBACK, port=args.port, log_level="info")
    return 0


def health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK}:{port}/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_env_file(path: Path) -> dict[str, str]:
    """Read the sentinel env file (`export KEY="value"` lines) the API maintains."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def run_collector_loop(args: argparse.Namespace) -> int:
    alive = parent_watch(args.parent_pid)
    while alive() and not health_ok(args.port):
        time.sleep(1)
    while alive():
        env = dict(os.environ)
        env_file = env.get("USAGE_TRACKER_ENV_FILE")
        if env_file:
            env.update(load_env_file(Path(env_file)))
        run_one_cycle(env, alive, timeout=max(args.interval * 5, 120))
        deadline = time.monotonic() + args.interval
        while alive() and time.monotonic() < deadline:
            time.sleep(1)
    return 0


def run_one_cycle(
    env: dict[str, str],
    alive: Callable[[], bool],
    *,
    timeout: float,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Run collect-once, but never outlive our own parent or the timeout.

    A blocking subprocess.run here would leave the loop deaf to a dead supervisor for as long
    as a cycle takes; polling keeps shutdown bounded by about a second.
    """
    proc = popen(
        self_command("collect-once", "--parent-pid", str(os.getpid())),
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    deadline = clock() + timeout
    while True:
        code = proc.poll()
        if code is not None:
            return code
        if not alive() or clock() > deadline:
            if alive():
                print("collect-once timed out", file=sys.stderr, flush=True)
            proc.kill()
            proc.wait()
            return None
        sleep(0.5)


def run_collect_once(args: argparse.Namespace) -> int:
    start_orphan_watchdog(parent_watch(args.parent_pid))
    from src import collector

    collector.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicur-backend", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    supervise = sub.add_parser("supervise", help="run API and collector, restart on death")
    supervise.add_argument("--port", type=int, default=DEFAULT_PORT)
    supervise.add_argument("--interval", type=int, default=COLLECTOR_INTERVAL)
    supervise.add_argument("--parent-pid", type=int, default=None)
    supervise.set_defaults(func=run_supervise)

    api = sub.add_parser("api", help="serve the API on 127.0.0.1")
    api.add_argument("--port", type=int, default=DEFAULT_PORT)
    api.add_argument("--parent-pid", type=int, default=None)
    api.set_defaults(func=run_api)

    loop = sub.add_parser("collector-loop", help="one collector cycle per interval")
    loop.add_argument("--port", type=int, default=DEFAULT_PORT)
    loop.add_argument("--interval", type=int, default=COLLECTOR_INTERVAL)
    loop.add_argument("--parent-pid", type=int, default=None)
    loop.set_defaults(func=run_collector_loop)

    once = sub.add_parser("collect-once", help="one collector cycle")
    once.add_argument("--parent-pid", type=int, default=None)
    once.set_defaults(func=run_collect_once)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
