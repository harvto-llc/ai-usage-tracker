"""Supervisor policy and helpers of packaging/backend/aicur_backend.py.

No real processes and no sleeping: popen and the clock are injected.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "packaging" / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import aicur_backend  # noqa: E402
from aicur_backend import ChildSpec, Supervisor  # noqa: E402


class FakeProc:
    def __init__(self, argv, stubborn=False):
        self.argv = argv
        self.code = None
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def exit(self, code):
        self.code = code

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.code = -15

    def kill(self):
        self.killed = True
        self.code = -9

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(specs=None, *, backoff=(1, 2, 4), stable_after=60, stubborn=()):
    specs = specs or [ChildSpec("api", ["api"]), ChildSpec("collector", ["loop"])]
    clock = Clock()
    started = []

    def popen(argv, **kwargs):
        proc = FakeProc(argv, stubborn=argv[0] in stubborn)
        started.append((argv, kwargs))
        return proc

    sup = Supervisor(
        specs,
        env={"USAGE_TRACKER_SECRET": "s"},
        popen=popen,
        clock=clock,
        backoff=backoff,
        stable_after=stable_after,
    )
    return sup, clock, started


def child(sup, name):
    return next(c for c in sup.children if c.spec.name == name)


def test_first_step_starts_every_child_with_env():
    sup, clock, started = make()
    sup.step()
    assert [argv for argv, _ in started] == [["api"], ["loop"]]
    assert all(kwargs["env"] == {"USAGE_TRACKER_SECRET": "s"} for _, kwargs in started)
    assert len(sup.running()) == 2


def test_quick_exits_back_off_then_cap():
    sup, clock, started = make(specs=[ChildSpec("api", ["api"])])
    sup.step()
    expected_delays = [1, 2, 4, 4]
    for delay in expected_delays:
        child(sup, "api").proc.exit(1)
        sup.step()  # notices the exit, schedules the restart
        assert child(sup, "api").proc is None
        clock.now += delay - 0.01
        sup.step()
        assert child(sup, "api").proc is None, f"restarted before {delay}s"
        clock.now += 0.01
        sup.step()
        assert child(sup, "api").proc is not None, f"not restarted after {delay}s"
    assert child(sup, "api").restarts == 4
    assert child(sup, "api").exits == [1, 1, 1, 1]
    assert len(started) == 5


def test_stable_run_resets_backoff():
    sup, clock, _ = make(specs=[ChildSpec("api", ["api"])])
    sup.step()
    for _ in range(3):  # build the delay up to the cap
        child(sup, "api").proc.exit(1)
        sup.step()
        clock.now += 10
        sup.step()
    clock.now += 60  # ran for a full stable_after window
    child(sup, "api").proc.exit(0)
    sup.step()
    assert child(sup, "api").next_start == pytest.approx(clock.now + 1)


def test_other_child_unaffected_by_a_crash():
    sup, clock, _ = make()
    sup.step()
    collector = child(sup, "collector").proc
    child(sup, "api").proc.exit(3)
    sup.step()
    assert child(sup, "collector").proc is collector
    assert child(sup, "api").exits == [3]


def test_stop_terminates_everything():
    sup, clock, _ = make()
    sup.step()
    procs = sup.running()
    sup.stop(grace=5)
    assert all(p.terminated for p in procs)
    assert not any(p.killed for p in procs)
    assert all(c.proc is None for c in sup.children)
    assert sup.running() == []


def test_stop_kills_a_stubborn_child_after_grace():
    sup, clock, _ = make(stubborn=("api",))
    sup.step()
    api = child(sup, "api").proc
    collector = child(sup, "collector").proc
    sup.stop(grace=5)
    assert api.terminated and api.killed
    assert collector.terminated and not collector.killed


def test_stop_after_child_already_died_is_quiet():
    sup, clock, _ = make()
    sup.step()
    dead = child(sup, "api").proc
    dead.exit(1)
    sup.stop(grace=5)
    assert not dead.terminated


def test_load_env_file(tmp_path):
    path = tmp_path / "env"
    path.write_text(
        'export A="x y"\n'
        "B=z\n"
        "C='q'\n"
        "# comment\n"
        "\n"
        "malformed line\n"
        "=novalue\n"
    )
    assert aicur_backend.load_env_file(path) == {"A": "x y", "B": "z", "C": "q"}
    assert aicur_backend.load_env_file(tmp_path / "missing") == {}


def test_self_command_source_mode():
    argv = aicur_backend.self_command("api", "--port", "9")
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "aicur_backend.py"
    assert argv[2:] == ["api", "--port", "9"]


def test_api_has_no_host_option():
    parser = aicur_backend.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["api", "--host", "0.0.0.0"])
    assert aicur_backend.LOOPBACK == "127.0.0.1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent watch")
def test_parent_watch_posix_tracks_original_ppid(monkeypatch):
    ppid = {"value": 4242}
    monkeypatch.setattr(os, "getppid", lambda: ppid["value"])
    alive = aicur_backend.parent_watch(None)
    assert alive()
    ppid["value"] = 1  # reparented to launchd/init: the launcher is gone
    assert not alive()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent watch")
def test_parent_watch_posix_launched_by_init_is_not_orphaned(monkeypatch):
    # A supervisor legitimately started with ppid 1 must not treat that as "parent gone".
    monkeypatch.setattr(os, "getppid", lambda: 1)
    assert aicur_backend.parent_watch(None)()


def test_pid_alive_for_self_and_nonsense():
    assert aicur_backend.pid_alive(os.getpid())
    assert not aicur_backend.pid_alive(0)
    assert not aicur_backend.pid_alive(-5)


class Ticker:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_one_cycle_returns_the_exit_code():
    procs = []

    def popen(argv, **kwargs):
        procs.append(FakeProc(argv))
        procs[-1].exit(0)
        return procs[-1]

    t = Ticker()
    assert aicur_backend.run_one_cycle({}, lambda: True, timeout=10, popen=popen,
                                       clock=t.clock, sleep=t.sleep) == 0
    argv = procs[0].argv
    assert argv[-3:] == ["collect-once", "--parent-pid", str(os.getpid())]


def test_one_cycle_kills_the_child_when_the_parent_dies():
    procs = []
    parent = {"alive": True}

    def popen(argv, **kwargs):
        procs.append(FakeProc(argv))
        return procs[-1]

    t = Ticker()

    def sleep(seconds):
        t.sleep(seconds)
        if t.now >= 2:
            parent["alive"] = False

    result = aicur_backend.run_one_cycle({}, lambda: parent["alive"], timeout=120, popen=popen,
                                         clock=t.clock, sleep=sleep)
    assert result is None
    assert procs[0].killed
    assert t.now <= 2.5  # bounded by the poll interval, not by the 120 s timeout


def test_one_cycle_kills_the_child_at_the_timeout():
    procs = []

    def popen(argv, **kwargs):
        procs.append(FakeProc(argv))
        return procs[-1]

    t = Ticker()
    assert aicur_backend.run_one_cycle({}, lambda: True, timeout=5, popen=popen,
                                       clock=t.clock, sleep=t.sleep) is None
    assert procs[0].killed


def test_collect_once_accepts_parent_pid():
    args = aicur_backend.build_parser().parse_args(["collect-once", "--parent-pid", "42"])
    assert args.parent_pid == 42
