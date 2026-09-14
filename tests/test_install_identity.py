"""The local half of install identity: one opaque id, stamped into both databases.

A MINIMAL install has two databases and only one of them can leave the machine.
`claude_usage.db` becomes a libsql embedded REPLICA when `TURSO_DATABASE_URL` is set
(`src/database.py:23-30`) and `sync()` pulls as well as pushes (`src/database.py:33-39`),
so an id stored inside it would be pulled down by the next install and two machines
would claim one identity. `~/.usage-tracker/activity.db` has no sync path. The id
therefore lives in neither, and these tests pin that down as a property rather than a
comment: the id has to survive the syncing database being deleted outright.

`libsql` is not installed in this environment, so `HAS_LIBSQL` is False and the Turso
code path cannot be executed here. That is exactly why the hazard is tested as
"the id does not depend on that database" instead of by driving a real replica.
"""

import ast
import os
import sqlite3
import stat
import sys
import threading
import time

# Pre-existing configuration that the tracker already required at the baseline commit
# (src/api.py:24-28) and that has nothing to do with identity. "Zero-config" below means
# this change adds NO new configuration: the install id needs no env var to start.
os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest

import src.database as db
from src import focus, install_identity, usage_ledger, work_ledger


# ── Where the id comes from ──────────────────────────────


def test_the_id_is_opaque_and_not_derived_from_any_ambient_value():
    value = install_identity.current()
    assert install_identity.is_known(value)
    # 128 bits of urandom, so it cannot have been built out of anything the machine
    # already knew. The negatives are the point: a hostname or an OS username collides
    # across machines and changes under the user's feet.
    assert value != os.uname().nodename
    assert value != os.environ.get("USER")
    assert value != os.environ.get("LOGNAME")


def test_the_id_generator_never_reaches_for_an_ambient_value():
    """Derived from the module's own syntax tree, not from a grep.

    A grep over the text matches the comment that explains why `uuid1` is wrong, so it
    would fail on a correct module and pass on a module that stopped explaining itself.
    The AST sees calls and imports and nothing else.
    """
    tree = ast.parse(open("src/install_identity.py", encoding="utf-8").read())

    imported = set()
    attributes = set()
    env_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                env_keys.add(node.args[0].value)

    assert imported.isdisjoint({"socket", "platform", "getpass", "pwd", "subprocess"})
    # uuid4 is os.urandom. uuid1 embeds the MAC address; uname/gethostname/getuser are
    # the machine telling you something it can change tomorrow.
    assert "uuid4" in attributes
    assert attributes.isdisjoint(
        {"uuid1", "uuid3", "uuid5", "getnode", "uname", "nodename", "gethostname", "getuser"}
    )
    # The ONLY thing the module reads out of the environment is where to put the file.
    assert env_keys == {"USAGE_TRACKER_INSTALL_ID_FILE"}


def test_the_id_is_generated_once_and_survives_a_restart(tmp_path):
    first = install_identity.current()
    # A "restart" is a fresh process, which is a cleared memo plus the same file.
    install_identity.reset_cache()
    second = install_identity.current()
    assert second == first
    assert install_identity.id_file_path().read_text(encoding="utf-8").strip() == first


def test_the_id_is_not_stored_in_the_syncing_database(tmp_path):
    """The hazard that decides the design, stated as a falsifiable property."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DB", tmp_path / "claude_usage.db")
        db.init()
        before = install_identity.current()
        assert install_identity.is_known(before)

        # Every table in the syncing database, and the database file itself, are gone.
        # If the id had been stored there, it could not come back.
        tables = {
            row[0]
            for row in sqlite3.connect(db.DB).execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in tables:
            with sqlite3.connect(db.DB) as conn:
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()
                }
            assert "install_id" in columns or name.startswith("sqlite_")
        db.DB.unlink()
        assert not db.DB.exists()

        install_identity.reset_cache()
        assert install_identity.current() == before


def test_the_id_is_not_stored_in_either_database(tmp_path, monkeypatch):
    """Neither database owns the identity, so neither can take it away.

    `claude_usage.db` is the urgent one because it replicates, but `activity.db` is
    excluded too: the id has to be IDENTICAL in both, so it cannot be owned by one of
    them and read by the other.
    """
    activity = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(activity))
    monkeypatch.setattr(db, "DB", tmp_path / "claude_usage.db")
    for module in (focus, usage_ledger, work_ledger):
        module._initialized_paths.discard(str(activity))

    db.init()
    focus.init()
    before = install_identity.current()
    assert install_identity.is_known(before)
    assert db.DB.exists() and activity.exists()

    db.DB.unlink()
    activity.unlink()
    install_identity.reset_cache()

    assert install_identity.current() == before


def test_two_installs_with_separate_id_files_do_not_share_an_identity(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("USAGE_TRACKER_INSTALL_ID_FILE", str(tmp_path / "a" / "install_id"))
        install_identity.reset_cache()
        first = install_identity.current()
        patch.setenv("USAGE_TRACKER_INSTALL_ID_FILE", str(tmp_path / "b" / "install_id"))
        install_identity.reset_cache()
        second = install_identity.current()
    assert install_identity.is_known(first)
    assert install_identity.is_known(second)
    assert first != second


def test_a_malformed_id_file_is_reported_absent_and_left_untouched(tmp_path, caplog):
    path = install_identity.id_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-an-install-id\n", encoding="utf-8")
    install_identity.reset_cache()

    assert install_identity.current() is None
    assert install_identity.stamp() == install_identity.UNKNOWN_INSTALL_ID
    # Not silently replaced. A regenerated id would be a brand new identity minted out
    # of a read failure, and the evidence of the failure would be gone.
    assert path.read_text(encoding="utf-8") == "not-an-install-id\n"


def test_an_unwritable_id_location_leaves_the_id_absent_without_raising(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("USAGE_TRACKER_INSTALL_ID_FILE", str(locked / "install_id"))
            install_identity.reset_cache()
            assert install_identity.current() is None
            assert install_identity.stamp() == install_identity.UNKNOWN_INSTALL_ID
    finally:
        locked.chmod(stat.S_IRWXU)


def test_concurrent_threads_in_one_process_share_one_id():
    """Threads only. `current()` holds `_cache_lock` across the whole mint, so this
    exercises the LOCK, not the file race - one process can never have two threads
    inside `_mint`. The cross-process race, which is the one that actually happens when
    launchd starts the API and the collector together, is covered by the two tests
    below; a module-level `threading.Lock` protects nothing across processes.
    """
    results: list[str | None] = []
    barrier = threading.Barrier(8)

    def race() -> None:
        barrier.wait()
        install_identity.reset_cache()
        results.append(install_identity.current())

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert all(install_identity.is_known(value) for value in results)
    assert len(set(results)) == 1


_CHILD_PRINTS_ID = (
    "import sys;"
    "sys.path.insert(0, {root!r});"
    "from src import install_identity as ii;"
    "print(ii.current())"
)

_CHILD_WAITS_THEN_PRINTS_ID = (
    "import os, sys, time;"
    "sys.path.insert(0, {root!r});"
    "from src import install_identity as ii;"
    "deadline = time.monotonic() + 10;"
    "[time.sleep(0.001) for _ in iter(lambda: not os.path.exists({go!r}) "
    "and time.monotonic() < deadline, False)];"
    "print(ii.current())"
)


def _child(script, path, tmp_path, **capture):
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-c", script],
        env={**os.environ, "USAGE_TRACKER_INSTALL_ID_FILE": str(path)},
        cwd=os.getcwd(),
        text=True,
        **capture,
    )


def test_the_id_file_is_never_observable_without_its_content(tmp_path):
    """Two REAL processes, because the bug this guards is not a thread bug.

    The naive mint - O_CREAT|O_EXCL, then write - creates the file and fills it in two
    steps, and between them the file exists and is EMPTY. A second process starting at
    the same moment opens a real file, reads nothing, and stamps `unknown` onto rows
    that would otherwise have carried the real id; those rows stay marked unknown even
    though the identity self-heals on the next call.

    So this spin-reads the path from this process while a child process mints, and
    requires that the path is NEVER seen existing with content that is not a
    well-formed id. Measured against the two-step version, a reader observed that empty
    window in 7 of 40 cold starts, so 25 trials leave it essentially no room to hide.
    """
    import subprocess

    root = os.getcwd()
    script = _CHILD_PRINTS_ID.format(root=root)
    trials = 25
    incomplete: list[str] = []
    minted = 0

    for trial in range(trials):
        path = tmp_path / f"trial-{trial}" / "install_id"
        path.parent.mkdir(parents=True)
        child = _child(script, path, tmp_path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        while child.poll() is None:
            try:
                text = path.read_bytes().decode("utf-8", "replace").strip()
            except FileNotFoundError:
                continue
            if not install_identity.is_known(text):
                incomplete.append(f"trial {trial}: saw {text!r}")
                break
        printed = (child.stdout.read() or "").strip()
        child.stdout.close()
        child.wait()
        if install_identity.is_known(printed):
            minted += 1

    # Without this the test could pass by never having observed anything at all.
    assert minted == trials, f"only {minted}/{trials} children minted an id"
    assert incomplete == []


def test_real_processes_racing_a_cold_start_all_report_the_same_id(tmp_path):
    """Six processes released together on an install that has no id yet.

    Every one of them must end up with the SAME well-formed id. A loser that reads an
    unfinished file reports None, which is what stamps `unknown` onto its rows.
    """
    import subprocess

    root = os.getcwd()
    path = tmp_path / "cold" / "install_id"
    path.parent.mkdir(parents=True)
    go = tmp_path / "go"
    script = _CHILD_WAITS_THEN_PRINTS_ID.format(root=root, go=str(go))

    children = [
        _child(script, path, tmp_path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for _ in range(6)
    ]
    time.sleep(0.4)  # let every child reach its wait loop before releasing them
    go.write_text("go", encoding="utf-8")

    reported = []
    for child in children:
        out = (child.stdout.read() or "").strip()
        child.stdout.close()
        child.wait()
        reported.append(out)

    assert len(reported) == 6
    assert all(install_identity.is_known(value) for value in reported), reported
    assert len(set(reported)) == 1, reported
    assert path.read_text(encoding="utf-8").strip() == reported[0]


def test_a_file_that_appears_during_the_check_is_read_not_called_malformed(monkeypatch):
    """Forced interleaving, because luck is not a test.

    `current()` reads the file, and only if that read finds nothing does it ask whether
    the file EXISTS - to tell "no id yet" from "an id that is corrupt". Another process
    can publish in the instant between those two steps, and then the file exists, is
    perfectly good, and would be called malformed. The loser stamps `unknown` onto rows
    whose id was fine.

    The real-process tests above cannot pin this down: once the publish itself is atomic
    the window is microseconds wide and they catch it only by luck. So the interleaving
    is reproduced exactly, by making the first read publish the file as it returns
    nothing - which is precisely what a second process does at that moment.
    """
    winner = "b" * 32
    assert install_identity.is_known(winner)

    path = install_identity.id_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    real_read = install_identity._read
    reads: list[object] = []

    def read_then_another_process_wins(target):
        reads.append(target)
        if len(reads) == 1:
            target.write_text(winner + "\n", encoding="utf-8")
            return None
        return real_read(target)

    monkeypatch.setattr(install_identity, "_read", read_then_another_process_wins)
    install_identity.reset_cache()

    assert install_identity.current() == winner
    assert install_identity.stamp() == winner
    # The interleaving actually happened: the first read found nothing and a second
    # read is what recovered the id.
    assert len(reads) >= 2
    # And the winner's file was not overwritten by a minted id of our own.
    assert path.read_text(encoding="utf-8").strip() == winner


# ── The truthiness rule, which this codebase has relearned three times ──


@pytest.mark.parametrize("value", [None, "", "0", 0, 0.0, False, "unknown", "0" * 31, "0" * 33, "A" * 32])
def test_is_known_rejects_every_value_that_is_not_a_well_formed_id(value):
    assert install_identity.is_known(value) is False


def test_is_known_accepts_a_real_id():
    assert install_identity.is_known(install_identity.current()) is True


def test_stamp_returns_the_reserved_marker_only_when_the_id_is_absent(monkeypatch):
    monkeypatch.setattr(install_identity, "current", lambda: None)
    assert install_identity.stamp() == install_identity.UNKNOWN_INSTALL_ID
    # The marker is unmistakable: it can never pass the id shape, so no reader can take
    # it for an install that exists.
    assert install_identity.is_known(install_identity.UNKNOWN_INSTALL_ID) is False

    # A falsy-but-present value is a value. `stamp()` must return it, not the marker.
    monkeypatch.setattr(install_identity, "current", lambda: "")
    assert install_identity.stamp() == ""
    monkeypatch.setattr(install_identity, "current", lambda: "0")
    assert install_identity.stamp() == "0"


# ── Migration on POPULATED databases ─────────────────────


_PRE_IDENTITY_SCHEMA = """
CREATE TABLE usage_samples(
    timestamp INTEGER NOT NULL, session_pct REAL NOT NULL, weekly_pct REAL NOT NULL,
    extra_pct REAL NOT NULL DEFAULT 0, session_reset TEXT, weekly_reset TEXT,
    extra_reset TEXT, extra_spent_usd REAL, extra_limit_usd REAL, extra_balance_usd REAL,
    weekly_sonnet_pct REAL, weekly_design_pct REAL, plan_id TEXT, raw_plan TEXT,
    plan_source TEXT, limits_json TEXT, credit_pools_json TEXT
);
CREATE TABLE codex_usage_samples(
    timestamp INTEGER NOT NULL, weekly_remaining_pct REAL, code_review_remaining_pct REAL,
    reset_at TEXT, plan_id TEXT, raw_plan TEXT, plan_source TEXT, limits_json TEXT,
    credit_pools_json TEXT, rate_limit_reset_credits INTEGER, credits_remaining REAL
);
CREATE TABLE provider_plan_history(
    provider TEXT NOT NULL, effective_at INTEGER NOT NULL, plan_id TEXT NOT NULL,
    raw_plan TEXT, source TEXT NOT NULL, PRIMARY KEY(provider, effective_at)
);
CREATE TABLE provider_metric_samples(
    timestamp INTEGER NOT NULL, day TEXT NOT NULL, provider TEXT NOT NULL,
    status TEXT NOT NULL, sample_hash TEXT NOT NULL, shared_json TEXT NOT NULL,
    unique_json TEXT NOT NULL, source_json TEXT NOT NULL, error_text TEXT,
    PRIMARY KEY(provider, timestamp)
);
"""

_STAMPED_TABLES = (
    "usage_samples",
    "codex_usage_samples",
    "provider_plan_history",
    "provider_metric_samples",
)


def _populate_pre_identity_usage_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(_PRE_IDENTITY_SCHEMA)
        conn.execute(
            "INSERT INTO usage_samples(timestamp, session_pct, weekly_pct, extra_pct) "
            "VALUES (?,?,?,?)",
            (1000, 12.5, 30.0, 0.0),
        )
        conn.execute(
            "INSERT INTO codex_usage_samples(timestamp, weekly_remaining_pct) VALUES (?,?)",
            (1001, 44.0),
        )
        conn.execute(
            "INSERT INTO provider_plan_history(provider, effective_at, plan_id, source) "
            "VALUES (?,?,?,?)",
            ("claude", 1002, "max-5x", "detected"),
        )
        conn.execute(
            "INSERT INTO provider_metric_samples("
            "timestamp, day, provider, status, sample_hash, shared_json, unique_json, source_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (1003, "2026-08-27", "claude", "fresh", "abc", "{}", "{}", "{}"),
        )


def test_a_populated_usage_database_migrates_without_losing_a_row(tmp_path):
    path = tmp_path / "claude_usage.db"
    _populate_pre_identity_usage_db(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DB", path)
        db.init()
        db.init()  # idempotent: a second start must not re-ALTER or duplicate

    with sqlite3.connect(path) as conn:
        for table in _STAMPED_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "install_id" in columns, table
            nulls = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE install_id IS NULL"
            ).fetchone()[0]
            assert nulls == 0, f"{table} left a row NULL-by-accident"


def test_rows_that_predate_the_column_are_marked_unknown_not_claimed_by_this_install(tmp_path):
    """A replica may already hold rows PULLED from another install.

    Backfilling those with the local id would fabricate provenance and then push the
    fabrication back to the remote on the next sync.
    """
    path = tmp_path / "claude_usage.db"
    _populate_pre_identity_usage_db(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DB", path)
        db.init()
        mine = install_identity.current()

    with sqlite3.connect(path) as conn:
        for table in _STAMPED_TABLES:
            stamped = conn.execute(f"SELECT install_id FROM {table}").fetchone()[0]
            assert stamped == install_identity.UNKNOWN_INSTALL_ID, table
            assert stamped != mine, table


def test_new_rows_in_the_syncing_database_carry_this_install_id(tmp_path):
    path = tmp_path / "claude_usage.db"
    _populate_pre_identity_usage_db(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DB", path)
        db.init()
        mine = install_identity.current()
        # A plan TRANSITION: the fixture already holds max-5x, and
        # record_plan_observation only writes when the plan actually changed.
        db.insert(1.0, 2.0, ts=2000, plan_id="max-20x", plan_source="detected")
        db.insert_codex(
            weekly_remaining_pct=50.0,
            code_review_remaining_pct=None,
            reset_at=None,
            ts=2001,
        )
        db.insert_provider_metric_sample(
            provider="codex", status="fresh", shared={}, unique={}, source={}, ts=2002
        )

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT install_id FROM usage_samples WHERE timestamp = 2000"
        ).fetchone()[0] == mine
        assert conn.execute(
            "SELECT install_id FROM codex_usage_samples WHERE timestamp = 2001"
        ).fetchone()[0] == mine
        assert conn.execute(
            "SELECT install_id FROM provider_metric_samples WHERE timestamp = 2002"
        ).fetchone()[0] == mine
        # provider_plan_history is written as a side effect of insert() when the plan
        # is known, so it is stamped through that path rather than a separate call.
        assert conn.execute(
            "SELECT install_id FROM provider_plan_history WHERE effective_at = 2000"
        ).fetchone()[0] == mine


def test_the_usage_database_migration_is_reversible(tmp_path):
    """Reversing THIS migration means dropping THIS column and nothing else.

    `init()` also applies older migrations that a pre-identity database was missing, so
    the baseline is the post-init shape minus `install_id`, not the shape the fixture
    was built with; comparing against the fixture would credit this change with undoing
    migrations it never made.
    """
    path = tmp_path / "claude_usage.db"
    _populate_pre_identity_usage_db(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "DB", path)
        db.init()
    with sqlite3.connect(path) as conn:
        migrated = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in _STAMPED_TABLES
        }
    assert all("install_id" in columns for columns in migrated.values())
    expected = {
        table: [name for name in columns if name != "install_id"]
        for table, columns in migrated.items()
    }

    with sqlite3.connect(path) as conn:
        for table in _STAMPED_TABLES:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN install_id")
        after = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in _STAMPED_TABLES
        }
        rows = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _STAMPED_TABLES
        }
    assert after == expected
    assert rows == {table: 1 for table in _STAMPED_TABLES}


# ── The non-syncing database ─────────────────────────────


@pytest.fixture
def activity_db(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(path))
    usage_ledger._initialized_paths.discard(str(path))
    work_ledger._initialized_paths.discard(str(path))
    focus._initialized_paths.discard(str(path))
    return path


_PLANS = {
    "claude": {
        "plan": "max-5x",
        "cost_usd_month": 100.0,
        "self_quota": {"weekly_cap_tokens": 1_000_000},
    }
}


def _seed_daily_model(path, *, day="2026-08-27"):
    usage_ledger.init()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO usage_daily_models(
                   provider, day, model, messages, user_messages, sessions, requests,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens, reasoning_tokens,
                   updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("claude", day, "claude-opus-4-8", 4, 2, 1, 3, 1000, 500, 200, 0, 0, 0, 0),
        )


def test_new_focus_facts_rows_carry_this_install_id(activity_db):
    _seed_daily_model(activity_db)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    mine = install_identity.current()

    with sqlite3.connect(activity_db) as conn:
        stamped = {row[0] for row in conn.execute("SELECT x_install_id FROM focus_facts")}
        count = conn.execute("SELECT COUNT(*) FROM focus_facts").fetchone()[0]
    assert count > 0
    assert stamped == {mine}


def test_a_populated_focus_facts_table_migrates_without_losing_a_row(activity_db):
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    with sqlite3.connect(activity_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM focus_facts").fetchone()[0]
        # Rewind to the pre-identity shape on a table that already holds rows.
        conn.execute("ALTER TABLE focus_facts DROP COLUMN x_install_id")
        assert "x_install_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(focus_facts)")
        }
    assert before > 0

    focus._initialized_paths.discard(str(activity_db))
    focus.init()

    with sqlite3.connect(activity_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM focus_facts").fetchone()[0] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM focus_facts WHERE x_install_id IS NULL"
        ).fetchone()[0] == 0
        marks = {row[0] for row in conn.execute("SELECT x_install_id FROM focus_facts")}
    assert marks == {install_identity.UNKNOWN_INSTALL_ID}


def test_the_identity_index_still_refuses_a_duplicate_across_the_install_id_migration(activity_db):
    """`x_install_id` is deliberately NOT part of `idx_focus_facts_identity_v2`.

    The index is the BACKSTOP, not the everyday dedupe: `emit_usage_rows` deletes the
    provider-day's telemetry rows before re-emitting, so a repeated emission never
    reaches the index. What the index catches is a second writer producing a row that is
    the same fact. A row written before this column existed carries `unknown` and the
    same fact written afterwards carries the real id, so putting the column in the index
    would make those two DIFFERENT identities and admit both - one fact stored twice.
    """
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    with sqlite3.connect(activity_db) as conn:
        # Rewind the emitted rows to the state a pre-migration database is left in.
        conn.execute(
            "UPDATE focus_facts SET x_install_id = ?", (install_identity.UNKNOWN_INSTALL_ID,)
        )
        existing = conn.execute(
            "SELECT service_provider_name, service_name, charge_category, "
            "charge_period_start, charge_period_end, billing_period_start, "
            "billing_period_end, billing_currency, x_model, x_source "
            "FROM focus_facts LIMIT 1"
        ).fetchone()
    assert existing is not None

    mine = install_identity.stamp()
    assert mine != install_identity.UNKNOWN_INSTALL_ID

    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(activity_db) as conn:
            conn.execute(
                """INSERT INTO focus_facts(
                       service_provider_name, service_name, charge_category,
                       charge_period_start, charge_period_end,
                       billing_period_start, billing_period_end,
                       billing_currency, x_model, x_source, x_install_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*existing, mine, 1),
            )

    with sqlite3.connect(activity_db) as conn:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_focus_facts_identity_v2",),
        ).fetchone()[0]
    assert "x_install_id" not in index_sql


def test_the_focus_csv_export_is_unchanged_by_the_stamp(activity_db):
    """The exported FOCUS column set is a separate decision and is not touched here.

    `tests/test_invoice_ingest.py` pins `x_Source` as the final column, so appending to
    the export would not be a silent additive change.
    """
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    header = focus.export_focus_csv("2026-08").splitlines()[0]
    assert header.split(",")[-1] == "x_Source"
    assert "x_InstallId" not in header


# ── Zero-config startup, which the free Lite tier depends on ──


def test_zero_config_startup_mints_the_identity_and_serves_requests(tmp_path, monkeypatch):
    """The real lifespan, with NO identity configuration of any kind.

    This is written so it cannot pass while startup is broken: it runs `src.api`'s
    actual lifespan (TestClient only invokes it as a context manager), then requires a
    real 200 from both an open and an authenticated route, then requires the id to have
    landed at the DEFAULT home-relative path that nothing configured.
    """
    from fastapi.testclient import TestClient

    from src.api import API_SECRET, app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Nothing tells the tracker where its identity lives, or that it should have one.
    monkeypatch.delenv("USAGE_TRACKER_INSTALL_ID_FILE", raising=False)
    monkeypatch.delenv("USAGE_TRACKER_ACTIVITY_DB", raising=False)
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    assert "USAGE_TRACKER_INSTALL_ID_FILE" not in os.environ
    assert "USAGE_TRACKER_ACTIVITY_DB" not in os.environ
    # Only the data file is redirected, so the suite does not write the developer's own
    # claude_usage.db. Nothing about identity is configured.
    monkeypatch.setattr(db, "DB", tmp_path / "claude_usage.db")
    install_identity.reset_cache()
    focus._initialized_paths.clear()
    usage_ledger._initialized_paths.clear()
    work_ledger._initialized_paths.clear()

    default_path = home / ".usage-tracker" / "install_id"
    assert not default_path.exists()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get(
            "/focus/facts",
            params={"period": "2026-08"},
            headers={"Authorization": f"Bearer {API_SECRET}"},
        )
        assert response.status_code == 200

        # Startup, not first write, is where the identity is established.
        assert default_path.exists()
        assert install_identity.id_file_path() == default_path
        minted = install_identity.current()
        assert install_identity.is_known(minted)
        assert default_path.read_text(encoding="utf-8").strip() == minted

        db.insert(1.0, 2.0, ts=3000)

    with sqlite3.connect(db.DB) as conn:
        assert conn.execute(
            "SELECT install_id FROM usage_samples WHERE timestamp = 3000"
        ).fetchone()[0] == minted


def test_a_second_zero_config_startup_reuses_the_same_identity(tmp_path, monkeypatch):
    """Restart is not a new install."""
    from fastapi.testclient import TestClient

    from src.api import app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USAGE_TRACKER_INSTALL_ID_FILE", raising=False)
    monkeypatch.delenv("USAGE_TRACKER_ACTIVITY_DB", raising=False)
    monkeypatch.setattr(db, "DB", tmp_path / "claude_usage.db")
    install_identity.reset_cache()

    with TestClient(app):
        first = install_identity.current()
    # A restart is a fresh process: the memo is gone, the file is not.
    install_identity.reset_cache()
    with TestClient(app):
        second = install_identity.current()

    assert install_identity.is_known(first)
    assert second == first


# ── Every writer, derived from the source rather than listed ──


_STAMPED_COLUMN = {
    "focus_facts": "x_install_id",
    "usage_samples": "install_id",
    "codex_usage_samples": "install_id",
    "provider_plan_history": "install_id",
    "provider_metric_samples": "install_id",
}


def _insert_column_lists():
    """Every `INSERT ... INTO <stamped table>(...)` found anywhere under src/.

    Derived from the source, not enumerated in this test, so a writer added later is
    caught the day it appears rather than the day someone remembers to update a list.
    """
    import pathlib
    import re

    pattern = re.compile(
        r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(" + "|".join(_STAMPED_COLUMN) + r")\s*\(([^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )
    found = []
    for path in sorted(pathlib.Path("src").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            columns = {part.strip() for part in match.group(2).split(",")}
            found.append((f"{path}:{line}", match.group(1), columns))
    return found


def test_every_writer_to_a_stamped_table_supplies_the_install_column():
    writers = _insert_column_lists()
    # The scan itself has to be working: if the regex found nothing, this test would
    # pass vacuously while every writer went unstamped.
    assert len(writers) >= 5, writers
    assert {table for _, table, _ in writers} == set(_STAMPED_COLUMN)
    for where, table, columns in writers:
        assert _STAMPED_COLUMN[table] in columns, f"{where} writes {table} without an install stamp"


def test_invoice_rows_carry_the_install_that_ingested_them(activity_db):
    """`unknown` must mean one thing only: the row predates the column.

    An invoice row is written by this install even though the FIGURE came from the
    vendor - `x_source` is what separates a billed number from a derived one. Letting
    invoice rows fall to the column default would collapse "ingested here" and
    "predates the column" into the same marker.
    """
    from pathlib import Path

    from src import invoice_ingest

    fixture = Path("tests/fixtures/invoices/claude-2026-08.csv").read_text(encoding="utf-8")
    report = invoice_ingest.ingest_csv(fixture)
    assert report["rows_ingested"] > 0
    assert report["rejections"] == []

    mine = install_identity.current()
    assert install_identity.is_known(mine)
    with sqlite3.connect(activity_db) as conn:
        stamped = {
            row[0]
            for row in conn.execute(
                "SELECT x_install_id FROM focus_facts WHERE x_source = ?",
                (focus.SOURCE_INVOICE,),
            )
        }
    assert stamped == {mine}
    assert install_identity.UNKNOWN_INSTALL_ID not in stamped
