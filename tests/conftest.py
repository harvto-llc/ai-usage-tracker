import os
import tempfile
import time
from pathlib import Path

import pytest

from src import focus, install_identity, session_search_index, usage_ledger, work_ledger

# ── The floor, established at IMPORT time rather than by a fixture ──────────────────
#
# `isolate_activity_db` below gives every test its own database. That is the right shape
# for isolation BETWEEN tests, and it was not enough. It is a fixture, so it can be
# disabled, renamed, or simply not reached - and while measuring its cost I renamed it
# myself, ran the suite, and put 7 real rows into this machine's production
# `~/.usage-tracker/activity.db` at 2026-08-29 07:35:09Z: five Usage rows for that day's
# provider-models and both providers' Purchase rows, written by `collector.main()` from
# inside `tests/test_fleet_push.py`. The row COUNT never moved, because emission is
# delete-then-insert; only `x_install_id` and `created_at` showed it.
#
# So the redirect is set here, at module scope, where deleting a fixture cannot reopen
# it, and `pytest_sessionstart` refuses to run at all if the resolved path is still the
# real one. A guard that depends on remembering to apply it is the failure mode this
# file has now hit three times.
def _real_home() -> Path:
    """The account's real home, read before anything below redirects HOME.

    POSIX: the password database, which HOME cannot fake. Windows has no pwd module, and
    Path.home() there reads USERPROFILE, not HOME, so that is what the sandbox must move.
    """
    try:
        import pwd
    except ImportError:
        return Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


_REAL_HOME = _real_home()
_SESSION_SANDBOX = Path(tempfile.mkdtemp(prefix="usage-tracker-suite-"))
(_SESSION_SANDBOX / "home" / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
(_SESSION_SANDBOX / "home" / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_SESSION_SANDBOX / "home")
os.environ["USERPROFILE"] = str(_SESSION_SANDBOX / "home")  # Path.home() on Windows
os.environ["USAGE_TRACKER_ACTIVITY_DB"] = str(_SESSION_SANDBOX / "activity.db")
os.environ["USAGE_TRACKER_INSTALL_ID_FILE"] = str(_SESSION_SANDBOX / "install_id")
os.environ["USAGE_TRACKER_FLEET_QUEUE_DB"] = str(_SESSION_SANDBOX / "fleet_queue.db")


def pytest_sessionstart(session):
    """Refuse to run at all if anything still resolves to the developer's real data.

    Asserted on the RESOLVED path, not on the environment variables that were supposed
    to set it, because the question is where a write would actually land.
    """
    for name, resolved in (
        ("focus", focus._db_path().resolve()),
        ("usage_ledger", usage_ledger._db_path().resolve()),
    ):
        if str(resolved).startswith(str(_REAL_HOME / ".usage-tracker")):
            raise pytest.UsageError(
                f"refusing to run: {name} resolves to the real database {resolved}. "
                "The suite writes FOCUS rows through src/collector.py:main."
            )
    if Path.home().resolve() == _REAL_HOME:
        raise pytest.UsageError(
            f"refusing to run: Path.home() is the real home {_REAL_HOME}. "
            "The suite reads real session history through usage_ledger.sync_provider."
        )


@pytest.fixture(autouse=True)
def isolate_system_keychain(monkeypatch):
    monkeypatch.setenv("USAGE_TRACKER_KEYCHAIN", "0")
    monkeypatch.setenv("USAGE_TRACKER_WORK_REFRESH_SCHEDULER", "0")


@pytest.fixture(autouse=True)
def isolate_install_identity(tmp_path, monkeypatch):
    """Keep the suite from minting an install id into the developer's real home.

    `database.init()` establishes the identity at startup, so without this every test
    that touches the database would create ~/.usage-tracker/install_id on whatever
    machine ran the suite. Each test gets its own file, and the memo is cleared on both
    sides so a test cannot inherit the previous test's id.
    """
    install_identity.reset_cache()
    monkeypatch.setenv("USAGE_TRACKER_INSTALL_ID_FILE", str(tmp_path / "install_id"))
    yield
    install_identity.reset_cache()


@pytest.fixture(autouse=True)
def isolate_fleet_queue(tmp_path, monkeypatch):
    """Keep the suite from writing a fleet push queue into the developer's real home.

    The same hazard `isolate_install_identity` guards against, and it has already bitten:
    a run of the FULL suite created `~/.usage-tracker/fleet_queue.db` on the developer's
    machine, while `tests/test_fleet_push.py` alone created nothing. That asymmetry is the
    point - the file that exercises fleet push isolated itself, and the pollution came
    from somewhere else exercising `src/collector.py:main`, which calls
    `fleet_push.record_cycle`. Isolating it inside one test file is therefore exactly the
    stale-enumeration failure this codebase keeps relearning: the guard has to live where
    every test gets it, not where the author remembered to put it.

    Empty is not a defence either. Even with no endpoint configured the queue file is
    CREATED, because reporting the unconfigured state reads the counts and reading the
    counts opens the database. A zero-row file in the developer's home is still the suite
    writing outside its sandbox.
    """
    monkeypatch.setenv("USAGE_TRACKER_FLEET_QUEUE_DB", str(tmp_path / "fleet_queue.db"))


@pytest.fixture(autouse=True)
def isolate_activity_db(tmp_path, monkeypatch):
    """Keep the suite from writing usage, work and FOCUS rows into the developer's home.

    The third instance of the hazard `isolate_fleet_queue` documents, and it arrived by
    exactly the route that comment predicted. `src/collector.py:main` now calls
    `focus_emit.record_cycle`, which emits into `~/.usage-tracker/activity.db`. The two
    tests that run `collector.main()` live in `tests/test_fleet_push.py`, which had no
    reason to know about FOCUS emission and did not isolate the activity database - so
    running the suite would have started emitting real FOCUS rows onto whatever machine
    ran it, from a test file about something else entirely.

    So the guard goes where every test gets it, not where the author remembered. The
    per-path init memos are cleared on both sides because they are keyed by path: a test
    that inherited the previous test's memo would skip `init()` against a database that
    has no tables yet.

    Files that need to inspect the database still set the same variable to their own
    `tmp_path`; setting it twice is harmless, and their value wins.
    """
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    # And an empty home, for the same reason and at the same moment. `sync_provider`
    # resolves its inputs from `Path.home()` at call time, so an isolated-but-EMPTY
    # activity database makes the indexer believe it has never seen any of the
    # developer's real sessions and reindex all of them: measured at 98.8s inside a
    # single `GET /stats`, walking 2645 real JSONL files and 8.8M JSON lines from
    # ~/.claude and ~/.codex. Isolating the database without isolating its inputs trades
    # one kind of leak for another - the suite stops writing to real data and starts
    # reading it.
    #
    # Named `_home` rather than `home`: `tests/test_install_identity.py` builds its own
    # `tmp_path / "home"` with a bare `mkdir()`, and colliding with it would break that
    # file from here.
    sandbox_home = tmp_path / "_home"
    (sandbox_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (sandbox_home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.setenv("USERPROFILE", str(sandbox_home))
    for module in (usage_ledger, work_ledger, focus, session_search_index):
        module._initialized_paths.discard(str(db_path))
    yield
    for module in (usage_ledger, work_ledger, focus, session_search_index):
        module._initialized_paths.discard(str(db_path))


@pytest.fixture
def local_timezone(monkeypatch):
    """Pin the process's local time zone for one test: `local_timezone("America/Los_Angeles")`.

    Tests that assert wall-clock strings (datetime.fromtimestamp, strftime) must name the zone
    they were written in; otherwise they pass only where the machine happens to sit in it.
    The previous zone is restored after the test.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable on this platform; cannot pin the local time zone")

    def pin(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield pin
    monkeypatch.undo()
    time.tzset()
