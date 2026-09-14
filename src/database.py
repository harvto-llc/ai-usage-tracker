import hashlib
import json
import sqlite3
import time
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src import install_identity

DB = Path(__file__).resolve().parent.parent / "claude_usage.db"

# Turso / LibSQL configuration (optional remote sync)
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

try:
    import libsql
    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False


def _connect():
    if HAS_LIBSQL and TURSO_URL:
        # Connect with remote sync capability
        return libsql.connect(str(DB), sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def sync() -> None:
    """Explicitly sync with remote Turso DB if configured."""
    if not HAS_LIBSQL or not TURSO_URL:
        return
    with closing(_connect()) as conn:
        if hasattr(conn, "sync"):
            conn.sync()


def init() -> None:
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_samples(
                timestamp INTEGER NOT NULL,
                session_pct REAL NOT NULL,
                weekly_pct REAL NOT NULL,
                extra_pct REAL NOT NULL DEFAULT 0,
                session_reset TEXT,
                weekly_reset TEXT,
                extra_reset TEXT,
                extra_spent_usd REAL,
                extra_limit_usd REAL,
                extra_balance_usd REAL,
                weekly_sonnet_pct REAL,
                weekly_design_pct REAL,
                plan_id TEXT,
                raw_plan TEXT,
                plan_source TEXT,
                limits_json TEXT,
                credit_pools_json TEXT,
                install_id TEXT NOT NULL DEFAULT 'unknown'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_usage_samples(
                timestamp INTEGER NOT NULL,
                weekly_remaining_pct REAL,
                code_review_remaining_pct REAL,
                reset_at TEXT,
                plan_id TEXT,
                raw_plan TEXT,
                plan_source TEXT,
                limits_json TEXT,
                credit_pools_json TEXT,
                rate_limit_reset_credits INTEGER,
                credits_remaining REAL,
                install_id TEXT NOT NULL DEFAULT 'unknown'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_plan_history(
                provider TEXT NOT NULL,
                effective_at INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                raw_plan TEXT,
                source TEXT NOT NULL,
                install_id TEXT NOT NULL DEFAULT 'unknown',
                PRIMARY KEY(provider, effective_at)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_metric_samples(
                timestamp INTEGER NOT NULL,
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                sample_hash TEXT NOT NULL,
                shared_json TEXT NOT NULL,
                unique_json TEXT NOT NULL,
                source_json TEXT NOT NULL,
                error_text TEXT,
                install_id TEXT NOT NULL DEFAULT 'unknown',
                PRIMARY KEY(provider, timestamp)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_metric_samples_provider_ts
            ON provider_metric_samples(provider, timestamp DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_metric_samples_day_provider
            ON provider_metric_samples(day, provider)
            """
        )
        # Lightweight migration for existing databases created before reset fields existed.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(usage_samples)").fetchall()}
        if "session_reset" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN session_reset TEXT")
        if "weekly_reset" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN weekly_reset TEXT")
        if "extra_pct" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN extra_pct REAL NOT NULL DEFAULT 0")
        if "extra_reset" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN extra_reset TEXT")
        if "extra_spent_usd" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN extra_spent_usd REAL")
        if "extra_limit_usd" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN extra_limit_usd REAL")
        if "extra_balance_usd" not in existing:
            conn.execute("ALTER TABLE usage_samples ADD COLUMN extra_balance_usd REAL")
        for col in ("weekly_sonnet_pct", "weekly_design_pct"):
            if col not in existing:
                conn.execute(f"ALTER TABLE usage_samples ADD COLUMN {col} REAL")
        for col in ("plan_id", "raw_plan", "plan_source", "limits_json", "credit_pools_json"):
            if col not in existing:
                conn.execute(f"ALTER TABLE usage_samples ADD COLUMN {col} TEXT")
        # Codex session fields migration
        codex_cols = {row[1] for row in conn.execute("PRAGMA table_info(codex_usage_samples)").fetchall()}
        if "session_remaining_pct" not in codex_cols:
            conn.execute("ALTER TABLE codex_usage_samples ADD COLUMN session_remaining_pct REAL")
        if "session_reset" not in codex_cols:
            conn.execute("ALTER TABLE codex_usage_samples ADD COLUMN session_reset TEXT")
        for col in ("weekly_gpt54_remaining_pct", "weekly_spark_remaining_pct"):
            if col not in codex_cols:
                conn.execute(f"ALTER TABLE codex_usage_samples ADD COLUMN {col} REAL")
        for col in ("plan_id", "raw_plan", "plan_source", "limits_json", "credit_pools_json"):
            if col not in codex_cols:
                conn.execute(f"ALTER TABLE codex_usage_samples ADD COLUMN {col} TEXT")
        if "rate_limit_reset_credits" not in codex_cols:
            conn.execute("ALTER TABLE codex_usage_samples ADD COLUMN rate_limit_reset_credits INTEGER")
        if "credits_remaining" not in codex_cols:
            conn.execute("ALTER TABLE codex_usage_samples ADD COLUMN credits_remaining REAL")
        provider_cols = {row[1] for row in conn.execute("PRAGMA table_info(provider_metric_samples)").fetchall()}
        if "day" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN day TEXT")
        if "status" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN status TEXT NOT NULL DEFAULT 'stale'")
        if "sample_hash" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN sample_hash TEXT NOT NULL DEFAULT ''")
        if "shared_json" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN shared_json TEXT NOT NULL DEFAULT '{}'")
        if "unique_json" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN unique_json TEXT NOT NULL DEFAULT '{}'")
        if "source_json" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN source_json TEXT NOT NULL DEFAULT '{}'")
        if "error_text" not in provider_cols:
            conn.execute("ALTER TABLE provider_metric_samples ADD COLUMN error_text TEXT")
        # Install identity, on a database that may already hold rows.
        #
        # `NOT NULL DEFAULT 'unknown'` fills every existing row in the one ALTER, so no
        # row is left NULL-by-accident. The default is deliberately NOT this install's
        # id: with TURSO_DATABASE_URL set this file is a libsql replica, so some of the
        # rows already in it may have been PULLED from a different install. Stamping
        # those with the local id would fabricate a provenance claim and then push it
        # back to the remote. `unknown` is the true statement about them, and it can
        # never be confused with a real id because a real id is 32 lowercase hex
        # characters (see src/install_identity.py).
        plan_cols = {row[1] for row in conn.execute("PRAGMA table_info(provider_plan_history)").fetchall()}
        for table, columns in (
            ("usage_samples", existing),
            ("codex_usage_samples", codex_cols),
            ("provider_plan_history", plan_cols),
            ("provider_metric_samples", provider_cols),
        ):
            if "install_id" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN install_id TEXT NOT NULL "
                    f"DEFAULT '{install_identity.UNKNOWN_INSTALL_ID}'"
                )
        conn.execute(
            """
            UPDATE provider_metric_samples
            SET day = date(timestamp, 'unixepoch', 'localtime')
            WHERE day IS NULL OR TRIM(day) = ''
            """
        )
        conn.commit()
    # Establish the install's identity at startup rather than on the first write, so a
    # tracker that came up is a tracker that already knows who it is. Returns None and
    # logs when it cannot be persisted; it never raises, because zero-config startup is
    # what the free Lite tier promises.
    install_identity.current()


def insert(
    session: float,
    weekly: float,
    extra: float = 0,
    ts: int | None = None,
    session_reset: str | None = None,
    weekly_reset: str | None = None,
    extra_reset: str | None = None,
    extra_spent_usd: float | None = None,
    extra_limit_usd: float | None = None,
    extra_balance_usd: float | None = None,
    weekly_sonnet_pct: float | None = None,
    weekly_design_pct: float | None = None,
    plan_id: str | None = None,
    raw_plan: str | None = None,
    plan_source: str | None = None,
    limits: list[dict] | None = None,
    credit_pools: list[dict] | None = None,
) -> None:
    timestamp = int(ts if ts is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO usage_samples(
                timestamp, session_pct, weekly_pct, extra_pct,
                session_reset, weekly_reset, extra_reset,
                extra_spent_usd, extra_limit_usd, extra_balance_usd,
                weekly_sonnet_pct, weekly_design_pct,
                plan_id, raw_plan, plan_source, limits_json, credit_pools_json,
                install_id
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                timestamp,
                session,
                weekly,
                extra,
                session_reset,
                weekly_reset,
                extra_reset,
                extra_spent_usd,
                extra_limit_usd,
                extra_balance_usd,
                weekly_sonnet_pct,
                weekly_design_pct,
                plan_id,
                raw_plan,
                plan_source,
                json.dumps(limits) if limits is not None else None,
                json.dumps(credit_pools) if credit_pools is not None else None,
                install_identity.stamp(),
            ),
        )
        conn.commit()
    if plan_id:
        record_plan_observation("claude", plan_id, raw_plan, plan_source or "unknown", timestamp)


def last_samples(n: int = 20) -> list[tuple[int, float, float, float]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT timestamp, session_pct, weekly_pct, COALESCE(extra_pct, 0)
            FROM usage_samples
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    return rows


def latest_sample() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                timestamp,
                session_pct,
                weekly_pct,
                COALESCE(extra_pct, 0),
                session_reset,
                weekly_reset,
                extra_reset,
                extra_spent_usd,
                extra_limit_usd,
                extra_balance_usd,
                weekly_sonnet_pct,
                weekly_design_pct,
                plan_id,
                raw_plan,
                plan_source,
                limits_json,
                credit_pools_json
            FROM usage_samples
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    extra = row[3]
    session_reset = row[4]
    weekly_reset = row[5]
    extra_reset = row[6]
    extra_spent_usd = row[7] if len(row) > 7 else None
    extra_limit_usd = row[8] if len(row) > 8 else None
    extra_balance_usd = row[9] if len(row) > 9 else None
    weekly_sonnet_pct = row[10] if len(row) > 10 else None
    weekly_design_pct = row[11] if len(row) > 11 else None
    plan_id = row[12] if len(row) > 12 else None
    raw_plan = row[13] if len(row) > 13 else None
    plan_source = row[14] if len(row) > 14 else None
    limits = json.loads(row[15]) if len(row) > 15 and row[15] else []
    credit_pools = json.loads(row[16]) if len(row) > 16 and row[16] else []
    if not weekly_reset:
        with closing(_connect()) as conn:
            prev = conn.execute(
                """
                SELECT weekly_reset
                FROM usage_samples
                WHERE weekly_reset IS NOT NULL AND TRIM(weekly_reset) <> ''
                ORDER BY timestamp DESC
                LIMIT 1
                """
            ).fetchone()
            if prev:
                weekly_reset = prev[0]
    if extra > 0 and not extra_reset:
        with closing(_connect()) as conn:
            prev = conn.execute(
                """
                SELECT extra_reset
                FROM usage_samples
                WHERE extra_reset IS NOT NULL AND TRIM(extra_reset) <> ''
                ORDER BY timestamp DESC
                LIMIT 1
                """
            ).fetchone()
            if prev:
                extra_reset = prev[0]
    if extra > 0 and not extra_reset:
        extra_reset = weekly_reset or session_reset
    return {
        "timestamp": row[0],
        "session": row[1],
        "weekly": row[2],
        "extra": extra,
        "session_reset": session_reset,
        "weekly_reset": weekly_reset,
        "extra_reset": extra_reset,
        "extra_spent_usd": extra_spent_usd,
        "extra_limit_usd": extra_limit_usd,
        "extra_balance_usd": extra_balance_usd,
        "weekly_sonnet_pct": weekly_sonnet_pct,
        "weekly_design_pct": weekly_design_pct,
        "plan_id": plan_id,
        "raw_plan": raw_plan,
        "plan_source": plan_source,
        "limits": limits,
        "credit_pools": credit_pools,
    }


def insert_codex(
    weekly_remaining_pct: float | None,
    code_review_remaining_pct: float | None,
    reset_at: str | None,
    session_remaining_pct: float | None = None,
    session_reset: str | None = None,
    weekly_gpt54_remaining_pct: float | None = None,
    weekly_spark_remaining_pct: float | None = None,
    plan_id: str | None = None,
    raw_plan: str | None = None,
    plan_source: str | None = None,
    limits: list[dict] | None = None,
    credit_pools: list[dict] | None = None,
    rate_limit_reset_credits: int | None = None,
    credits_remaining: float | None = None,
    ts: int | None = None,
) -> None:
    timestamp = int(ts if ts is not None else time.time())
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO codex_usage_samples(
                timestamp, weekly_remaining_pct, code_review_remaining_pct,
                reset_at, session_remaining_pct, session_reset,
                weekly_gpt54_remaining_pct, weekly_spark_remaining_pct,
                plan_id, raw_plan, plan_source, limits_json, credit_pools_json,
                rate_limit_reset_credits, credits_remaining, install_id
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (timestamp, weekly_remaining_pct, code_review_remaining_pct,
             reset_at, session_remaining_pct, session_reset,
             weekly_gpt54_remaining_pct, weekly_spark_remaining_pct,
             plan_id, raw_plan, plan_source,
             json.dumps(limits) if limits is not None else None,
             json.dumps(credit_pools) if credit_pools is not None else None,
             rate_limit_reset_credits, credits_remaining,
             install_identity.stamp()),
        )
        conn.commit()
    if plan_id:
        record_plan_observation("codex", plan_id, raw_plan, plan_source or "unknown", timestamp)


def latest_codex() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT timestamp, weekly_remaining_pct, code_review_remaining_pct,
                   reset_at, session_remaining_pct, session_reset,
                   weekly_gpt54_remaining_pct, weekly_spark_remaining_pct,
                   plan_id, raw_plan, plan_source, limits_json, credit_pools_json,
                   rate_limit_reset_credits, credits_remaining
            FROM codex_usage_samples
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    return {
        "timestamp": row[0],
        "weekly_remaining_pct": row[1],
        "code_review_remaining_pct": row[2],
        "reset_at": row[3],
        "session_remaining_pct": row[4] if len(row) > 4 else None,
        "session_reset": row[5] if len(row) > 5 else None,
        "weekly_gpt54_remaining_pct": row[6] if len(row) > 6 else None,
        "weekly_spark_remaining_pct": row[7] if len(row) > 7 else None,
        "plan_id": row[8] if len(row) > 8 else None,
        "raw_plan": row[9] if len(row) > 9 else None,
        "plan_source": row[10] if len(row) > 10 else None,
        "limits": json.loads(row[11]) if len(row) > 11 and row[11] else [],
        "credit_pools": json.loads(row[12]) if len(row) > 12 and row[12] else [],
        "rate_limit_reset_credits": row[13] if len(row) > 13 else None,
        "credits_remaining": row[14] if len(row) > 14 else None,
    }


def codex_reset_credit_balance_changes(start_ts: int, end_ts: int | None = None) -> dict:
    """Summarize observed Codex banked-reset balance changes in a period."""
    end = int(end_ts if end_ts is not None else time.time())
    with closing(_connect()) as conn:
        baseline = conn.execute(
            """
            SELECT timestamp, rate_limit_reset_credits
            FROM codex_usage_samples
            WHERE rate_limit_reset_credits IS NOT NULL AND timestamp < ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (int(start_ts),),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT timestamp, rate_limit_reset_credits
            FROM codex_usage_samples
            WHERE rate_limit_reset_credits IS NOT NULL
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (int(start_ts), end),
        ).fetchall()

    observations = ([baseline] if baseline else []) + rows
    decreases = 0
    increases = 0
    for previous, current in zip(observations, observations[1:]):
        delta = int(current[1]) - int(previous[1])
        if delta < 0:
            decreases += -delta
        elif delta > 0:
            increases += delta

    return {
        "balance_decreases": decreases,
        "balance_increases": increases,
        "starting_balance": int(observations[0][1]) if observations else None,
        "ending_balance": int(observations[-1][1]) if observations else None,
    }


def record_plan_observation(
    provider: str,
    plan_id: str,
    raw_plan: str | None,
    source: str,
    ts: int | None = None,
) -> bool:
    """Record a plan transition, returning True only when the plan changed."""
    timestamp = int(ts if ts is not None else time.time())
    with closing(_connect()) as conn:
        previous = conn.execute(
            "SELECT plan_id, raw_plan, source FROM provider_plan_history "
            "WHERE provider = ? ORDER BY effective_at DESC LIMIT 1",
            (provider,),
        ).fetchone()
        current = (plan_id, raw_plan, source)
        if previous and tuple(previous) == current:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO provider_plan_history("
            "provider, effective_at, plan_id, raw_plan, source, install_id) "
            "VALUES (?,?,?,?,?,?)",
            (provider, timestamp, plan_id, raw_plan, source, install_identity.stamp()),
        )
        conn.commit()
    return True


def plan_history(provider: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT effective_at, plan_id, raw_plan, source FROM provider_plan_history "
            "WHERE provider = ? ORDER BY effective_at",
            (provider,),
        ).fetchall()
    return [
        {"effective_at": row[0], "plan_id": row[1], "raw_plan": row[2], "source": row[3]}
        for row in rows
    ]


def last_codex_samples(limit: int = 5) -> list[tuple]:
    """Return recent codex samples: (timestamp, session_remaining_pct)."""
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT timestamp, session_remaining_pct FROM codex_usage_samples "
                "WHERE session_remaining_pct IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows
    except sqlite3.OperationalError:
        return []


def samples_in_range(start_ts: int, end_ts: int) -> list[tuple]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT
                timestamp, session_pct, weekly_pct, COALESCE(extra_pct, 0),
                session_reset, weekly_reset, extra_reset,
                extra_spent_usd, extra_limit_usd, extra_balance_usd
            FROM usage_samples
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
            """,
            (start_ts, end_ts),
        ).fetchall()
    return rows


_cc_cache: dict = {"data": None, "ts": 0}


def cc_messages_today() -> dict:
    """Cached wrapper around shared scanner."""
    now = time.time()
    if _cc_cache["data"] is not None and now - _cc_cache["ts"] < 60:
        return _cc_cache["data"]
    from src.scanners import scan_cc_messages_today
    result = scan_cc_messages_today()
    _cc_cache["data"] = result
    _cc_cache["ts"] = now
    return result


def cc_token_usage_today() -> dict:
    from src.scanners import scan_cc_tokens_today
    return scan_cc_tokens_today()


_cc_token_cache: dict = {"data": None, "ts": 0}


def cc_token_usage_today_cached() -> dict:
    now = time.time()
    if _cc_token_cache["data"] is not None and now - _cc_token_cache["ts"] < 60:
        return _cc_token_cache["data"]
    result = cc_token_usage_today()
    _cc_token_cache["data"] = result
    _cc_token_cache["ts"] = now
    return result


def codex_local_stats() -> dict:
    """Read Codex local SQLite for thread/token data."""
    db_path = Path.home() / ".codex" / "state_5.sqlite"
    if not db_path.exists():
        return {}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            # Total stats
            row = conn.execute("SELECT SUM(tokens_used), COUNT(*) FROM threads").fetchone()
            total_tokens = row[0] or 0
            total_sessions = row[1] or 0

            # Per model
            model_rows = conn.execute(
                "SELECT model, SUM(tokens_used), COUNT(*) FROM threads GROUP BY model"
            ).fetchall()

            # Recent threads
            recent = conn.execute(
                """SELECT title, tokens_used, model, source,
                          datetime(updated_at, 'unixepoch', 'localtime')
                   FROM threads ORDER BY updated_at DESC LIMIT 5"""
            ).fetchall()

            # Per surface (CLI / Web / IDE)
            source_rows = conn.execute(
                "SELECT source, SUM(tokens_used), COUNT(*) FROM threads GROUP BY source"
            ).fetchall()

        return {
            "total_tokens": total_tokens,
            "total_sessions": total_sessions,
            "by_model": {r[0] or "unknown": {"tokens": r[1], "sessions": r[2]} for r in model_rows},
            "by_source": {(r[0] or "unknown"): {"tokens": r[1] or 0, "sessions": r[2]} for r in source_rows},
            "recent_threads": [
                {"title": r[0][:60], "tokens": r[1], "model": r[2], "source": r[3], "updated": r[4]}
                for r in recent
            ],
        }
    except Exception:
        return {}


def load_claude_code_stats() -> dict | None:
    stats_file = Path.home() / ".claude" / "stats-cache.json"
    if not stats_file.exists():
        return None
    try:
        with open(stats_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _used_pct_from_remaining(remaining_pct: float | None) -> float | None:
    if remaining_pct is None:
        return None
    try:
        used_pct = 100 - float(remaining_pct)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, used_pct))


def _last_completed_cycle_peak(
    used_values: list[float | None],
    *,
    reset_drop_threshold: float,
    reset_floor: float,
) -> float | None:
    previous: float | None = None
    segment_peak: float | None = None
    last_completed: float | None = None

    for value in used_values:
        if value is None:
            continue

        is_reset = False
        if previous is not None:
            drop = previous - value
            is_reset = drop > reset_drop_threshold or (drop > 0.5 and value <= reset_floor)

        if is_reset:
            if segment_peak is not None:
                last_completed = segment_peak
            segment_peak = value
        else:
            segment_peak = value if segment_peak is None else max(segment_peak, value)
        previous = value

    return round(last_completed, 1) if last_completed is not None else None


def codex_last_completed_cycles(limit: int = 5000) -> dict[str, float | None]:
    """Return previous completed Codex session/weekly cycle peak usage percentages."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT session_remaining_pct, weekly_remaining_pct
            FROM codex_usage_samples
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    ordered = list(reversed(rows))
    session_values = [_used_pct_from_remaining(row[0]) for row in ordered]
    weekly_values = [_used_pct_from_remaining(row[1]) for row in ordered]
    return {
        "session_used_pct": _last_completed_cycle_peak(
            session_values,
            reset_drop_threshold=10,
            reset_floor=2,
        ),
        "weekly_used_pct": _last_completed_cycle_peak(
            weekly_values,
            reset_drop_threshold=20,
            reset_floor=5,
        ),
    }


def insert_provider_metric_sample(
    *,
    provider: str,
    status: str,
    shared: dict | None,
    unique: dict | None,
    source: dict | None,
    error_text: str | None = None,
    ts: int | None = None,
    heartbeat_seconds: int = 3600,
) -> bool:
    provider = str(provider or "").strip().lower()
    if not provider:
        return False
    status = str(status or "stale").strip().lower() or "stale"
    timestamp = int(ts if ts is not None else time.time())
    day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    shared_json = json.dumps(shared or {}, sort_keys=True, separators=(",", ":"))
    unique_json = json.dumps(unique or {}, sort_keys=True, separators=(",", ":"))
    source_json = json.dumps(source or {}, sort_keys=True, separators=(",", ":"))
    sample_hash = _provider_sample_hash(status=status, shared_json=shared_json, unique_json=unique_json)

    with closing(_connect()) as conn:
        previous = conn.execute(
            """
            SELECT timestamp, status, sample_hash
            FROM provider_metric_samples
            WHERE provider = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
        if previous:
            previous_ts, previous_status, previous_hash = int(previous[0]), previous[1], previous[2]
            if (
                previous_hash == sample_hash
                and previous_status == status
                and (timestamp - previous_ts) < heartbeat_seconds
            ):
                return False

        conn.execute(
            """
            INSERT OR REPLACE INTO provider_metric_samples(
                timestamp, day, provider, status, sample_hash, shared_json, unique_json,
                source_json, error_text, install_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                day,
                provider,
                status,
                sample_hash,
                shared_json,
                unique_json,
                source_json,
                error_text,
                install_identity.stamp(),
            ),
        )
        conn.commit()
        return True


def insert_provider_metric_samples(
    snapshots: list[dict],
    *,
    heartbeat_seconds: int = 3600,
) -> int:
    inserted = 0
    for snapshot in snapshots:
        if insert_provider_metric_sample(
            provider=snapshot.get("provider", ""),
            status=snapshot.get("status", "stale"),
            shared=snapshot.get("shared"),
            unique=snapshot.get("unique"),
            source=snapshot.get("source"),
            error_text=snapshot.get("error_text"),
            ts=snapshot.get("timestamp"),
            heartbeat_seconds=heartbeat_seconds,
        ):
            inserted += 1
    return inserted


def latest_provider_metric_samples() -> dict[str, dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT provider, timestamp, day, status, shared_json, unique_json, source_json, error_text
            FROM provider_metric_samples
            ORDER BY provider ASC, timestamp DESC
            """
        ).fetchall()

    latest: dict[str, dict] = {}
    for row in rows:
        provider = row[0]
        if provider in latest:
            continue
        latest[provider] = {
            "provider": provider,
            "timestamp": int(row[1]),
            "day": row[2],
            "status": row[3],
            "shared": _safe_json_load(row[4], {}),
            "unique": _safe_json_load(row[5], {}),
            "source": _safe_json_load(row[6], {}),
            "error_text": row[7],
        }
    return latest


def prune_provider_metric_samples(retention_days: int = 180, now_ts: int | None = None) -> int:
    timestamp = int(now_ts if now_ts is not None else time.time())
    cutoff = timestamp - max(1, retention_days) * 86400
    with closing(_connect()) as conn:
        cursor = conn.execute(
            "DELETE FROM provider_metric_samples WHERE timestamp < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
    return int(deleted)


def _provider_sample_hash(*, status: str, shared_json: str, unique_json: str) -> str:
    digest = hashlib.sha256()
    digest.update(status.encode("utf-8"))
    digest.update(b"\n")
    digest.update(shared_json.encode("utf-8"))
    digest.update(b"\n")
    digest.update(unique_json.encode("utf-8"))
    return digest.hexdigest()


def _safe_json_load(value: str | None, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    if parsed is None:
        return fallback
    return parsed
