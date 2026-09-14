"""The agent half of fleet mode: buffer locally, push OTLP-shaped, account for delivery.

WHAT THIS IS, AND WHAT IT IS NOT. There is no exporter, no OTLP client and no push
direction anywhere in this repository or in FULL. The roadmap's claim that
`FULL src/otel_receiver.py` "is the part already written for centralized push" is wrong
in direction: every OTLP symbol in FULL is on the RECEIVING side (`@app.post("/v1/metrics")`
at `FULL src/api.py:1537`; `process_metrics_payload` / `process_logs_payload` /
`flush_snapshot` in `FULL src/otel_receiver.py`). Re-measured on this branch:
`git grep -li -e otlp -e opentelemetry -- 'src/*.py' 'tests/*.py'` exits 1 in MINIMAL.
So this module is WRITTEN, not ported.

WHERE THE WIRE CONTRACT COMES FROM. It is derived by reading the receiver that will
accept it, at a pinned SHA, not invented and not validated only against a local double:

    repository  ~/dev_projects/usage-tracker      (FULL, read only, never imported)
    commit      2f5ca9e7065135f260a5816328af0f78b6fa1292
    file        src/otel_receiver.py

and specifically these lines of `process_metrics_payload` and its helpers:

  * the envelope it walks is `resourceMetrics[] -> scopeMetrics[] -> metrics[]`
    (`otel_receiver.py:75-79`);
  * a metric's data lives under `sum`, `gauge` or `histogram`, whichever is present
    (`metric.get("sum") or metric.get("gauge") or metric.get("histogram")`,
    `otel_receiver.py:81`), then `dataPoints` (`otel_receiver.py:82`);
  * `_extract_value` (`otel_receiver.py:227-241`) reads `asInt` FIRST and passes it to
    `int()`, so an int64 sent as a JSON string - which is what the OTLP JSON encoding
    requires - is accepted; `asDouble` goes to `float()`;
  * `_parse_attributes` (`otel_receiver.py:244-256`) expects a LIST of
    `{"key": ..., "value": {"stringValue"|"intValue"|"doubleValue"|"boolValue": ...}}`
    and flattens every value with `str()`;
  * the route replies `200` with `{"status": "ok", "dataPoints": <int>}`, and `400`
    "invalid JSON" on a body it cannot parse (`FULL src/api.py:1537-1551`).

WHY THE METRIC NAMES ARE OURS AND THE INSTRUMENT IS A GAUGE. The receiver's
`claude_code.*` names are accumulated ADDITIVELY - `_token_usage[model][type] += ...`
(`otel_receiver.py:88-91`) - because Claude Code exports them with DELTA temporality.
What this agent has to hand is the running total for TODAY, taken from a fresh scan of
local state each cycle. Publishing a cumulative number under a name the receiver adds up
would multiply it by the number of cycles in the day. So the values go out under
`usage_tracker.*` as `gauge`, which is the instrument that means "the value right now".
A name the receiver does not recognise still lands in its `counts` tally
(`counts[name] += 1`, `otel_receiver.py:85`) and so is still counted in the `dataPoints`
figure that comes back, which is the only receiver-side number this module trusts.

DELIVERY IS ACCOUNTED FOR, AND NOT BY US. A record is buffered, or it is delivered with
a receipt; never both and never neither. The receipt is built out of what the RECEIVER
said it observed and nothing else. A `sent=1` column the sender writes for itself is the
same defect as a `compensation_verified` flag, and it was already FAILed once on this
lineage. Concretely, only two things are acknowledgements:

  1. the receiver names the record ids it accepted (`acceptedRecordIds`), or
  2. the receiver reports the number of data points it counted (`dataPoints`) and that
     number EQUALS the number sent.

A 200 whose body carries neither is not an acknowledgement. A 200 whose count disagrees
is not an acknowledgement. In both cases the record stays buffered with the reason
recorded, because leaving it buffered risks a duplicate the receiver can detect, while
marking it delivered loses the row silently.

RETRY IS AT-LEAST-ONCE, ON PURPOSE, AND CARRIES ITS OWN DEDUPE KEY. An acknowledgement
can be lost after the receiver has already counted the batch, so a resend is possible by
construction and no local bookkeeping can prevent it. Every data point therefore carries
`usage_tracker.record.id`, stable across every attempt, so the receiver can recognise a
record it has already taken. This is stated rather than hidden: the receiver at
`2f5ca9e` does NOT dedupe - it accumulates - so against THAT receiver a resend
double-counts, and `tests/test_fleet_push.py` asserts that outcome directly instead of
implying it away.

ABSENT CONFIGURATION IS A STATE, NOT SILENCE. No endpoint configured is the NORMAL
condition today, because the receiver does not exist yet. Every entry point returns a
state dict naming what it decided and why. Nothing here raises into a caller, nothing
blocks, and no local surface changes shape when push is unconfigured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from src import install_identity

logger = logging.getLogger(__name__)


# ── The states this module can be in, all of them announced ──────────────

STATE_UNCONFIGURED = "unconfigured"
STATE_DISABLED = "disabled"
STATE_READY = "ready"
STATE_IDLE = "idle"
STATE_PUSHED = "pushed"

REASON_ENDPOINT_ABSENT = "endpoint_absent"
# Not a fleet member, so there is nothing to deliver and nothing to buffer. Distinct
# from an endpoint that exists and is unreachable, which is what buffering is FOR.
REASON_NOT_A_FLEET_MEMBER = "not_a_fleet_member"
REASON_EXPLICITLY_DISABLED = "explicitly_disabled"
REASON_NOTHING_PENDING = "nothing_pending"
# The backlog hit its cap and the oldest undelivered records were dropped. This is a
# LOSS and it is reported as one; a queue that silently sheds its oldest rows is the
# number that quietly shrinks.
REASON_BACKLOG_CAPPED = "backlog_cap_discarded_oldest"
# Everything still buffered is held by another sender's live claim. Distinct from
# having nothing to send, because the two call for different operator responses.
REASON_CLAIMED_ELSEWHERE = "batch_claimed_elsewhere"
REASON_ENDPOINT_CONFIGURED = "endpoint_configured"

# Why a batch that was actually attempted did not come back acknowledged.
REASON_UNREACHABLE = "receiver_unreachable"
REASON_HTTP_ERROR = "receiver_http_error"
REASON_UNREADABLE_BODY = "receiver_body_unreadable"
REASON_NO_EVIDENCE = "receiver_reported_no_evidence"
REASON_COUNT_MISMATCH = "receiver_count_mismatch"
# The receiver named the records it took and ours was not among them. It DID supply
# evidence, so calling this "no evidence" would misreport what happened.
REASON_PARTIALLY_ACCEPTED = "receiver_did_not_accept_this_record"

ACK_BY_RECORD_IDS = "record-ids"
ACK_BY_DATA_POINT_COUNT = "data-point-count"


# ── Configuration. Every one of these is optional. ───────────────────────

ENDPOINT_ENV = "USAGE_TRACKER_FLEET_ENDPOINT"
TOKEN_ENV = "USAGE_TRACKER_FLEET_TOKEN"
ENABLED_ENV = "USAGE_TRACKER_FLEET_PUSH"
QUEUE_DB_ENV = "USAGE_TRACKER_FLEET_QUEUE_DB"
TIMEOUT_ENV = "USAGE_TRACKER_FLEET_TIMEOUT"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_BATCH_LIMIT = 200

# HOW BIG THE BACKLOG MAY GET, derived from the cadence rather than picked.
#
# The collector runs every 60 seconds under launchd (`src/collector.py:5`), so one day of
# undelivered records is 1440 of them. Measured on a real populated queue: 1440 records
# occupy 1,142,784 bytes, which is 793.6 bytes per record and 1.09 MiB per day. That
# figure was produced independently and matches the supervisor's own measurement of the
# same shape to the byte-per-row.
#
# Seven days is the tolerance. A receiver outage longer than a week is an operational
# failure rather than a blip, and the underlying numbers are NOT lost when a record is
# discarded: the same usage lives in `claude_usage.db` and `~/.usage-tracker/activity.db`,
# which are the sources of truth. This queue is a TRANSPORT buffer, and a transport buffer
# that grows without bound is a leak, not durability.
COLLECTOR_CYCLES_PER_DAY = 24 * 60
BACKLOG_DAYS = 7
DEFAULT_MAX_BUFFERED_RECORDS = COLLECTOR_CYCLES_PER_DAY * BACKLOG_DAYS  # 10080, ~7.6 MiB

# Acknowledged records are kept only so an operator can read recent receipts. They are
# delivered; the lifetime counter remembers them after the rows are gone.
DEFAULT_DELIVERED_RETENTION = 1000

MAX_BUFFERED_ENV = "USAGE_TRACKER_FLEET_MAX_BUFFERED"
DELIVERED_RETENTION_ENV = "USAGE_TRACKER_FLEET_DELIVERED_RETENTION"

# The scope name is ours; `FULL src/otel_receiver.py` never reads `scope.name`, but a
# real OTLP consumer does, and emitting Claude Code's scope for rows we produced would
# be a false provenance claim.
SCOPE_NAME = "com.amgad.usage-tracker.fleet"

INSTALL_ID_ATTRIBUTE = "usage_tracker.install.id"
EXPORT_ID_ATTRIBUTE = "usage_tracker.export.id"
RECORD_ID_ATTRIBUTE = "usage_tracker.record.id"

# The OTLP metrics proto's Gauge message has EXACTLY ONE field, `data_points`; the proto
# says in so many words that Gauge does not support different aggregation temporalities,
# and `aggregation_temporality` is a field of Sum and Histogram, not of Gauge. See
# opentelemetry/proto/metrics/v1/metrics.proto in open-telemetry/opentelemetry-proto.
#
# An earlier version of this module emitted `aggregationTemporality` inside the gauge.
# The pinned FULL receiver accepted it, because it reads `dataPoints` and ignores unknown
# sibling keys (otel_receiver.py:81-82) - which is exactly why a contract test written
# against that lenient parser could not catch it, and why the schema is asserted here as
# a key set instead. Caught in peer review of dd55015.
GAUGE_FIELDS = frozenset({"dataPoints"})

# NumberDataPoint's fields, for the same reason. `asInt` and `asDouble` are the two arms
# of one `oneof value`, so at most one of them may appear on a single data point.
NUMBER_DATA_POINT_FIELDS = frozenset({
    "attributes", "startTimeUnixNano", "timeUnixNano", "asInt", "asDouble", "exemplars", "flags",
})

_OFF_VALUES = {"0", "false", "no", "off"}


def _flag_is_off(name: str) -> bool:
    """True only when the flag is present AND says off.

    `os.environ.get(name)` returning `''` means the variable is set to empty, which is
    not one of the off words, so an empty value leaves push enabled. Absent leaves it
    enabled too. Only an explicit off word disables.
    """
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _OFF_VALUES


def endpoint() -> str | None:
    """The configured receiver URL, or None.

    A path/URL is configuration, not a value carrying identity, provenance or
    measurement, so an empty string legitimately means "no location". The standing
    truthiness rule governs the values we BUFFER, and those are checked against `None`
    everywhere below - see `_measurement`.
    """
    raw = os.environ.get(ENDPOINT_ENV)
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    return trimmed


def token() -> str | None:
    """The machine token, if one is configured.

    Read from the environment and never written to a file by this module. The governing
    spec (`ai-cur@4bfa2a3:specs/usage-tracker-tenancy/spec.md`, decision D3) puts the
    receiver-issued machine token in the OS keychain; the environment is how a launchd
    job or an operator hands it in without one existing on disk. Absent is allowed: the
    collector already posts without an `Authorization` header when no secret is set
    (`src/collector.py:275-277`), and this follows that established shape rather than
    inventing a second convention.
    """
    raw = os.environ.get(TOKEN_ENV)
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    return trimmed


def timeout_seconds() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if parsed <= 0:
        logger.warning("%s=%r is not positive; using %s", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return parsed


# ── The durable buffer ───────────────────────────────────────────────────

def queue_path() -> Path:
    """Where buffered records live.

    Its own file, and deliberately neither of the two databases this install already has.
    `claude_usage.db` becomes a libsql embedded REPLICA when `TURSO_DATABASE_URL` is set
    (`src/database.py:25-32`) and `sync()` PULLS as well as pushes (`src/database.py:35-41`),
    so a queue kept there would arrive on a second install and be pushed twice by two
    machines. `~/.usage-tracker/activity.db` is redirectable per process by
    `USAGE_TRACKER_ACTIVITY_DB` (`src/focus.py:72-76`), so a queue keyed to it would move
    whenever the data file moved, stranding records that were never delivered.
    """
    configured = os.environ.get(QUEUE_DB_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".usage-tracker" / "fleet_queue.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_queue(
    record_id    TEXT PRIMARY KEY,
    install_id   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    data_points  INTEGER NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    receipt_json TEXT,
    claimed_at   INTEGER
)
"""

# Lifetime totals that OUTLIVE the rows they count. Pruning a delivered record must not
# make the delivered figure fall, and discarding an undeliverable one must not make the
# loss disappear; a count that quietly shrinks is the failure deliverable 4 exists to
# prevent. The rows are evidence and are bounded; these numbers are history and are not.
_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_stats(
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
)
"""

STAT_DELIVERED_TOTAL = "delivered_total"
STAT_DISCARDED_TOTAL = "discarded_total"

# The one column that decides delivery. NULL means still buffered; a receipt means the
# receiver's own evidence arrived. There is no third column and no sender-written flag,
# so "both" and "neither" are not representable states.
_UNDELIVERED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_push_queue_undelivered
ON push_queue(created_at) WHERE receipt_json IS NULL
"""


def _connect() -> sqlite3.Connection:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    with closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        # A queue file written before the claim column existed. Additive, and the column
        # is nullable because "never claimed" is the honest default for those rows.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(push_queue)").fetchall()}
        if "claimed_at" not in columns:
            conn.execute("ALTER TABLE push_queue ADD COLUMN claimed_at INTEGER")
        conn.execute(_STATS_SCHEMA)
        conn.execute(_UNDELIVERED_INDEX)
        conn.commit()


def claim_ttl_seconds() -> float:
    """How long one sender's claim on a batch stands before another may take it.

    Comfortably longer than a request can take, so a claim never expires under a sender
    that is merely slow; short enough that a sender killed mid-flight does not strand
    records. A claim is a LEASE, not a lock: a crashed process must not be able to make a
    record undeliverable forever, because the record is real data that has to go out.
    """
    return max(timeout_seconds() * 3, 60.0)


def claim_batch(limit: int = DEFAULT_BATCH_LIMIT, *, now: int | None = None) -> list[dict]:
    """Take exclusive ownership of a batch of buffered records, atomically.

    WHY THIS EXISTS. `pending()` alone let two senders read the same rows and send them
    both, which is a duplicate that has nothing to do with the receiver and everything to
    do with this module. The collector runs every 60 seconds under launchd, so two cycles
    can overlap whenever one push is slow, and a duplicate that the agent creates itself
    is not covered by "at-least-once is irreducible". Found in peer review of dd55015.

    The SELECT and the UPDATE are one `BEGIN IMMEDIATE` transaction, so the write lock is
    taken before the rows are read and a second sender either waits or sees the rows
    already claimed. It is never both.
    """
    init()
    stamp = int(now if now is not None else time.time())
    cutoff = stamp - int(claim_ttl_seconds())
    conn = _connect()
    # Explicit transaction control: the default implicit handling would not let us hold
    # the write lock across the SELECT, which is the whole point.
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT record_id, install_id, created_at, metrics_json, data_points,
                   attempts, last_error
            FROM push_queue
            WHERE receipt_json IS NULL
              AND (claimed_at IS NULL OR claimed_at <= ?)
            ORDER BY created_at, record_id
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE push_queue SET claimed_at = ? WHERE record_id = ?",
                [(stamp, row["record_id"]) for row in rows],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return [_as_record(row) for row in rows]


def release_claim(record_ids: list[str]) -> None:
    """Give a batch back, so the next cycle retries it immediately rather than waiting
    out the lease. Only ever clears the claim; never touches the receipt."""
    if not record_ids:
        return
    with closing(_connect()) as conn:
        conn.executemany(
            "UPDATE push_queue SET claimed_at = NULL WHERE record_id = ? AND receipt_json IS NULL",
            [(record_id,) for record_id in record_ids],
        )
        conn.commit()


def _as_record(row) -> dict:
    return {
        "record_id": row["record_id"],
        "install_id": row["install_id"],
        "created_at": row["created_at"],
        "metrics": json.loads(row["metrics_json"]),
        "data_points": row["data_points"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
    }


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default
    if parsed <= 0:
        logger.warning("%s=%r is not positive; using %s", name, raw, default)
        return default
    return parsed


def max_buffered_records() -> int:
    return _positive_int_env(MAX_BUFFERED_ENV, DEFAULT_MAX_BUFFERED_RECORDS)


def delivered_retention() -> int:
    return _positive_int_env(DELIVERED_RETENTION_ENV, DEFAULT_DELIVERED_RETENTION)


def _bump(conn, key: str, amount: int) -> None:
    if amount <= 0:
        return
    conn.execute(
        """
        INSERT INTO push_stats(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
        """,
        (key, amount),
    )


def _stat(conn, key: str) -> int:
    row = conn.execute("SELECT value FROM push_stats WHERE key = ?", (key,)).fetchone()
    # `is None` on the ROW, not truthiness on the value: a recorded total of 0 is a fact
    # this install measured, and it is not the same as never having recorded one.
    if row is None:
        return 0
    return row["value"]


def enforce_bounds() -> dict:
    """Keep the queue bounded. Returns what it had to throw away, and says so.

    Two different operations, and only one of them is a loss:

    - DELIVERED records beyond the retention window are PRUNED. Nothing is lost: they
      carry receipts, they have left the machine, and `delivered_total` remembers them.
    - UNDELIVERED records beyond the cap are DISCARDED, oldest first, and that IS a loss.
      It is counted in `discarded_total`, logged at warning, and named in the state every
      caller gets back. The oldest go because for a daily-total gauge the newest reading
      supersedes the older ones anyway, so the stale end is the cheap end.
    """
    init()
    discarded = 0
    pruned = 0
    with closing(_connect()) as conn:
        cap = max_buffered_records()
        buffered = conn.execute(
            "SELECT COUNT(*) AS n FROM push_queue WHERE receipt_json IS NULL"
        ).fetchone()["n"]
        if buffered > cap:
            discarded = buffered - cap
            conn.execute(
                """
                DELETE FROM push_queue WHERE record_id IN (
                    SELECT record_id FROM push_queue
                    WHERE receipt_json IS NULL
                    ORDER BY created_at, record_id
                    LIMIT ?
                )
                """,
                (discarded,),
            )
            _bump(conn, STAT_DISCARDED_TOTAL, discarded)
            logger.warning(
                "fleet push backlog hit its cap of %s records; discarded the %s oldest "
                "undelivered records, %s lost in total on this install",
                cap,
                discarded,
                _stat(conn, STAT_DISCARDED_TOTAL),
            )

        keep = delivered_retention()
        delivered_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM push_queue WHERE receipt_json IS NOT NULL"
        ).fetchone()["n"]
        if delivered_rows > keep:
            pruned = delivered_rows - keep
            conn.execute(
                """
                DELETE FROM push_queue WHERE record_id IN (
                    SELECT record_id FROM push_queue
                    WHERE receipt_json IS NOT NULL
                    ORDER BY created_at, record_id
                    LIMIT ?
                )
                """,
                (pruned,),
            )
        conn.commit()
    return {"discarded": discarded, "pruned_delivered": pruned}


def _measurement(name: str, value, attributes: dict | None = None) -> dict | None:
    """One reading, or None when there is genuinely nothing to report.

    `value is None`, never `if not value`. `0`, `0.0` and `''` are all falsy and none of
    them mean absent; a token count of exactly zero is a measurement and has to travel.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass. Sending True as asInt=1 would silently retype a flag
        # into a measurement, so refuse rather than guess.
        raise TypeError(f"measurement {name!r} is a bool; a flag is not a measurement")
    if not isinstance(value, (int, float)):
        raise TypeError(f"measurement {name!r} has non-numeric value {value!r}")
    return {
        "name": name,
        "value": value,
        "attributes": dict(attributes or {}),
    }


def measurements(readings) -> list[dict]:
    """Build the measurement list, dropping only the readings that are genuinely absent.

    `readings` is an iterable of `(name, value)` or `(name, value, attributes)`.
    """
    built = []
    for reading in readings:
        if len(reading) == 2:
            name, value = reading
            attributes = None
        else:
            name, value, attributes = reading
        one = _measurement(name, value, attributes)
        if one is not None:
            built.append(one)
    return built


def _canonical(install_id: str, timestamp: int, records: list[dict]) -> str:
    """A record id derived from the record's own content.

    Derived, not enumerated: a caller cannot hand in an id that disagrees with what it
    is sending, and re-buffering the identical cycle is an INSERT OR IGNORE rather than
    a second row. It is also what the receiver sees as `usage_tracker.record.id`, so the
    key the receiver would dedupe on and the key we key our own buffer on are the same
    string by construction.
    """
    blob = json.dumps(
        {"install_id": install_id, "timestamp": timestamp, "metrics": records},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def enqueue(readings, *, timestamp: int | None = None) -> str | None:
    """Buffer one cycle's readings durably. Returns its record id, or None if empty.

    Buffering happens whether or not push is configured, because the receiver being
    absent is the normal condition and the whole point is that nothing is lost while it
    is. This is also why buffering is not gated on `endpoint()`.
    """
    built = measurements(readings)
    if not built:
        return None
    ts = int(timestamp if timestamp is not None else time.time())
    stamped = install_identity.stamp()
    record_id = _canonical(stamped, ts, built)
    init()
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO push_queue(
                record_id, install_id, created_at, metrics_json, data_points
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (record_id, stamped, ts, json.dumps(built, sort_keys=True), len(built)),
        )
        conn.commit()
    # Enforced on every insert, not on a schedule, so the cap holds no matter who
    # enqueues and no matter how the process dies afterwards.
    enforce_bounds()
    return record_id


def pending(limit: int = DEFAULT_BATCH_LIMIT) -> list[dict]:
    """Buffered records that carry no receiver evidence, oldest first.

    Deliberately does NOT filter on `claimed_at`. A record another sender is holding is
    still buffered - it has no receipt - and reporting it as gone would be the same
    self-certification error as a `sent` flag. Claiming is about who SENDS; the receipt
    is what decides DELIVERY.
    """
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT record_id, install_id, created_at, metrics_json, data_points,
                   attempts, last_error
            FROM push_queue
            WHERE receipt_json IS NULL
            ORDER BY created_at, record_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_as_record(row) for row in rows]


def receipt(record_id: str) -> dict | None:
    """What the RECEIVER said about this record, or None while it is still buffered."""
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT receipt_json FROM push_queue WHERE record_id = ?", (record_id,)
        ).fetchone()
    if row is None:
        return None
    raw = row["receipt_json"]
    # `is None`, not falsiness: the receipt is evidence, and an empty string would be a
    # corrupt receipt rather than an absent one.
    if raw is None:
        return None
    return json.loads(raw)


def counts() -> dict:
    """How many records are buffered and how many carry receiver evidence."""
    init()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN receipt_json IS NULL THEN 1 ELSE 0 END) AS buffered,
                SUM(CASE WHEN receipt_json IS NOT NULL THEN 1 ELSE 0 END) AS delivered
            FROM push_queue
            """
        ).fetchone()
    total = row["total"]
    # SUM over zero rows is NULL, not 0. `or 0` would be wrong on a real 0 elsewhere;
    # here the only non-numeric result is NULL, so test for it explicitly.
    buffered = row["buffered"] if row["buffered"] is not None else 0
    delivered = row["delivered"] if row["delivered"] is not None else 0
    with closing(_connect()) as conn:
        delivered_total = _stat(conn, STAT_DELIVERED_TOTAL)
        discarded_total = _stat(conn, STAT_DISCARDED_TOTAL)
    return {
        # RETAINED rows, which are bounded and may be pruned.
        "total": total,
        "buffered": buffered,
        "delivered": delivered,
        # LIFETIME, which never falls. `delivered_total` outlives the pruned rows and
        # `discarded_total` is the honest record of what the cap threw away.
        "delivered_total": delivered_total,
        "discarded_total": discarded_total,
    }


# ── The wire shape, derived from FULL 2f5ca9e src/otel_receiver.py ────────

def _attributes(pairs: dict) -> list[dict]:
    """OTLP attribute array. `_parse_attributes` flattens every value with `str()`."""
    return [{"key": key, "value": {"stringValue": str(value)}} for key, value in sorted(pairs.items())]


def _data_point(record: dict, reading: dict, nanos: str) -> dict:
    """One OTLP data point.

    An int goes out as `asInt` carrying a STRING, which is what the OTLP JSON encoding
    requires for int64 and what `_extract_value` handles: it reads `asInt` first and
    passes it straight to `int()` (`FULL@2f5ca9e src/otel_receiver.py:229-230`).
    """
    value = reading["value"]
    if isinstance(value, int):
        encoded = {"asInt": str(value)}
    else:
        encoded = {"asDouble": float(value)}
    attributes = dict(reading.get("attributes") or {})
    # Stamped per data point, not only per resource: a receiver that fans data points out
    # of the envelope must still be able to attribute and dedupe each one.
    attributes[RECORD_ID_ATTRIBUTE] = record["record_id"]
    attributes[INSTALL_ID_ATTRIBUTE] = record["install_id"]
    point = {
        "attributes": _attributes(attributes),
        "startTimeUnixNano": nanos,
        "timeUnixNano": nanos,
    }
    point.update(encoded)
    return point


def build_export(records: list[dict], *, export_id: str) -> dict:
    """The OTLP HTTP/JSON body for a batch of buffered records.

    `gauge`, not `sum`: see the module docstring. The receiver accepts either
    (`metric.get("sum") or metric.get("gauge") or ...`, FULL@2f5ca9e
    `src/otel_receiver.py:81`), and `gauge` is the truthful instrument for a
    running-total-for-today reading.
    """
    metrics: dict[str, list[dict]] = {}
    order: list[str] = []
    for record in records:
        nanos = str(int(record["created_at"]) * 1_000_000_000)
        for reading in record["metrics"]:
            name = reading["name"]
            if name not in metrics:
                metrics[name] = []
                order.append(name)
            metrics[name].append(_data_point(record, reading, nanos))

    install_ids = sorted({record["install_id"] for record in records})
    resource_attributes = {
        EXPORT_ID_ATTRIBUTE: export_id,
        # One install per agent by construction; joined only so a batch that somehow
        # spans two is visible rather than silently reporting one of them.
        INSTALL_ID_ATTRIBUTE: ",".join(install_ids),
    }
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attributes(resource_attributes)},
                "scopeMetrics": [
                    {
                        "scope": {"name": SCOPE_NAME},
                        "metrics": [
                            {
                                "name": name,
                                # `dataPoints` and NOTHING ELSE. See GAUGE_FIELDS.
                                "gauge": {"dataPoints": metrics[name]},
                            }
                            for name in order
                        ],
                    }
                ],
            }
        ]
    }


def data_point_count(export: dict) -> int:
    """Count the data points in an export the same way the receiver counts them.

    The receiver's `dataPoints` reply is `sum(counts.values())` where `counts[name]`
    is incremented once per data point it walks (`FULL@2f5ca9e
    src/otel_receiver.py:85`, `FULL src/api.py:1547`). This walks the identical path, so
    the number we compare against is the number the receiver would have produced rather
    than an independent guess about our own payload.
    """
    total = 0
    for resource_metrics in export.get("resourceMetrics", []):
        for scope_metrics in resource_metrics.get("scopeMetrics", []):
            for metric in scope_metrics.get("metrics", []):
                data = metric.get("sum") or metric.get("gauge") or metric.get("histogram") or {}
                total += len(data.get("dataPoints", []))
    return total


# ── Acknowledgement: only the receiver's own evidence counts ─────────────

def read_acknowledgement(body: bytes, *, sent_record_ids: list[str], sent_data_points: int) -> dict:
    """Turn a receiver response body into an acknowledgement, or into a refusal.

    Returns `{"acknowledged": [...], "receipt": {...}}` when the receiver produced
    evidence, or `{"acknowledged": [], "reason": ...}` when it did not. NOTHING here
    consults what the sender believes it sent beyond the two figures it is comparing
    against, and no branch returns an acknowledgement without a receiver-supplied number
    or list to justify it.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"acknowledged": [], "reason": REASON_UNREADABLE_BODY}
    if not isinstance(parsed, dict):
        return {"acknowledged": [], "reason": REASON_UNREADABLE_BODY}

    # Strongest evidence: the receiver names what it took.
    if "acceptedRecordIds" in parsed:
        accepted = parsed["acceptedRecordIds"]
        if not isinstance(accepted, list):
            return {"acknowledged": [], "reason": REASON_UNREADABLE_BODY}
        named = [rid for rid in sent_record_ids if rid in set(accepted)]
        return {
            "acknowledged": named,
            # A receiver that names an empty set HAS supplied evidence: it took nothing.
            # Reporting that as "no evidence" would misdescribe a refusal as a silence,
            # and the two need different operator responses.
            "reason": REASON_PARTIALLY_ACCEPTED,
            "receipt": {
                "kind": ACK_BY_RECORD_IDS,
                "observed_record_ids": sorted(set(accepted)),
            },
        }

    # Weaker but still receiver-observed: how many data points it counted.
    # `in`, not truthiness: a reported count of 0 is a number the receiver produced, and
    # it has to be compared rather than treated as an absent field.
    if "dataPoints" in parsed:
        raw = parsed["dataPoints"]
        try:
            observed = int(raw)
        except (TypeError, ValueError):
            return {"acknowledged": [], "reason": REASON_UNREADABLE_BODY}
        if observed != sent_data_points:
            return {
                "acknowledged": [],
                "reason": REASON_COUNT_MISMATCH,
                "observed_data_points": observed,
            }
        return {
            "acknowledged": list(sent_record_ids),
            "receipt": {
                "kind": ACK_BY_DATA_POINT_COUNT,
                "observed_data_points": observed,
            },
        }

    return {"acknowledged": [], "reason": REASON_NO_EVIDENCE}


def _record_receipt(record_ids: list[str], receipt_body: dict) -> None:
    if not record_ids:
        return
    payload = json.dumps(receipt_body, sort_keys=True)
    with closing(_connect()) as conn:
        cursor = conn.cursor()
        newly_delivered = 0
        for record_id in record_ids:
            cursor.execute(
                """
                UPDATE push_queue
                SET receipt_json = ?, last_error = NULL
                WHERE record_id = ? AND receipt_json IS NULL
                """,
                (payload, record_id),
            )
            # Only rows this call actually moved from buffered to delivered. Counting the
            # request instead would double-count a record whose receipt already existed.
            newly_delivered += cursor.rowcount
        _bump(conn, STAT_DELIVERED_TOTAL, newly_delivered)
        conn.commit()
    enforce_bounds()


def _record_failure(record_ids: list[str], reason: str) -> None:
    if not record_ids:
        return
    with closing(_connect()) as conn:
        conn.executemany(
            """
            UPDATE push_queue
            SET attempts = attempts + 1, last_error = ?
            WHERE record_id = ? AND receipt_json IS NULL
            """,
            [(reason, record_id) for record_id in record_ids],
        )
        conn.commit()


# ── The decision, always announced ───────────────────────────────────────

def push_state() -> dict:
    """What this install would do about pushing right now, and why.

    Called for its own sake by an operator or a health surface. It performs no network
    request and never raises. A skip is a fail-open unless it announces itself, so the
    unconfigured branch is a first-class returned state and is tested as one.
    """
    if _flag_is_off(ENABLED_ENV):
        return {
            "state": STATE_DISABLED,
            "reason": REASON_EXPLICITLY_DISABLED,
            "endpoint": endpoint(),
            "counts": counts(),
        }
    target = endpoint()
    if target is None:
        return {
            "state": STATE_UNCONFIGURED,
            "reason": REASON_ENDPOINT_ABSENT,
            "endpoint": None,
            "counts": counts(),
        }
    return {
        "state": STATE_READY,
        "reason": REASON_ENDPOINT_CONFIGURED,
        "endpoint": target,
        "authenticated": token() is not None,
        "counts": counts(),
    }


def _post(target: str, body: bytes) -> tuple[int, bytes] | str:
    """Send one batch. Returns (status, body) or a failure reason string."""
    headers = {"Content-Type": "application/json"}
    machine_token = token()
    if machine_token is not None:
        headers["Authorization"] = f"Bearer {machine_token}"
    request = urllib.request.Request(target, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds()) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is the receiver refusing, which is not evidence that it took
        # anything. Read the body anyway so a receiver that explains itself is not
        # discarded, but never treat a non-2xx as an acknowledgement.
        try:
            exc.read()
        except Exception:  # noqa: BLE001 - the body is a nicety, its failure is not
            pass
        return REASON_HTTP_ERROR
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return REASON_UNREACHABLE


def flush(*, limit: int = DEFAULT_BATCH_LIMIT) -> dict:
    """Push what is buffered. Returns a state; never raises, never blocks unbounded.

    Every early return is a named state, so "did not push" is always distinguishable
    from "was never asked".
    """
    state = push_state()
    if state["state"] in (STATE_DISABLED, STATE_UNCONFIGURED):
        return {**state, "sent": 0, "acknowledged": 0}

    target = state["endpoint"]
    # Claimed, not merely read: two overlapping collector cycles must not both send the
    # same records. A skip because someone else holds the batch is its own named state,
    # distinguishable from having nothing to send at all.
    batch = claim_batch(limit=limit)
    if not batch:
        still_buffered = counts()["buffered"]
        return {
            "state": STATE_IDLE,
            "reason": REASON_CLAIMED_ELSEWHERE if still_buffered else REASON_NOTHING_PENDING,
            "endpoint": target,
            "sent": 0,
            "acknowledged": 0,
            "counts": counts(),
        }

    export_id = hashlib.sha256(
        ",".join(record["record_id"] for record in batch).encode("utf-8")
    ).hexdigest()
    export = build_export(batch, export_id=export_id)
    sent_record_ids = [record["record_id"] for record in batch]
    sent_data_points = data_point_count(export)
    body = json.dumps(export).encode("utf-8")

    outcome = _post(target, body)
    if isinstance(outcome, str):
        _record_failure(sent_record_ids, outcome)
        release_claim(sent_record_ids)
        return {
            "state": STATE_READY,
            "reason": outcome,
            "endpoint": target,
            "sent": len(sent_record_ids),
            "acknowledged": 0,
            "counts": counts(),
        }

    status, response_body = outcome
    if not (200 <= status < 300):
        _record_failure(sent_record_ids, REASON_HTTP_ERROR)
        release_claim(sent_record_ids)
        return {
            "state": STATE_READY,
            "reason": REASON_HTTP_ERROR,
            "endpoint": target,
            "status": status,
            "sent": len(sent_record_ids),
            "acknowledged": 0,
            "counts": counts(),
        }

    verdict = read_acknowledgement(
        response_body,
        sent_record_ids=sent_record_ids,
        sent_data_points=sent_data_points,
    )
    acknowledged = verdict["acknowledged"]
    if not acknowledged:
        reason = verdict.get("reason", REASON_NO_EVIDENCE)
        _record_failure(sent_record_ids, reason)
        release_claim(sent_record_ids)
        return {
            "state": STATE_READY,
            "reason": reason,
            "endpoint": target,
            "status": status,
            "sent": len(sent_record_ids),
            "acknowledged": 0,
            "counts": counts(),
        }

    receipt_body = {
        **verdict["receipt"],
        "status": status,
        "endpoint": target,
        "export_id": export_id,
        "sent_data_points": sent_data_points,
        "observed_at": int(time.time()),
    }
    _record_receipt(acknowledged, receipt_body)
    unacknowledged = [rid for rid in sent_record_ids if rid not in set(acknowledged)]
    if unacknowledged:
        _record_failure(unacknowledged, REASON_PARTIALLY_ACCEPTED)
        release_claim(unacknowledged)
    return {
        "state": STATE_PUSHED,
        "reason": verdict["receipt"]["kind"],
        "endpoint": target,
        "status": status,
        "sent": len(sent_record_ids),
        "acknowledged": len(acknowledged),
        "counts": counts(),
    }


def record_cycle(readings, *, timestamp: int | None = None, limit: int = DEFAULT_BATCH_LIMIT) -> dict:
    """Buffer this cycle then try to push. The boundary that never raises into a caller.

    `src/collector.py` calls exactly this. Local collection has to keep working when the
    fleet half is unconfigured, unreachable, slow or broken - that is the local-first
    promise in the business repo's `docs/deployment-privacy.md` - so every failure in
    here becomes a returned state with a reason, and none of them becomes an exception
    in the collector's cycle.
    """
    # AN INSTALL WITH NO ENDPOINT IS NOT A FLEET MEMBER, AND BUFFERS NOTHING.
    #
    # This is the one behaviour the supervisor overturned, and the measurement is why.
    # Buffering here ran on EVERY install, because no receiver exists yet, so
    # `unconfigured` is not an edge case - it is the state of every Lite install. Measured
    # over 1440 cycles, one day at the collector's 60s cadence, in an isolated queue with
    # no endpoint set: 1440 rows retained, 100% never acknowledged, 1,142,784 bytes, which
    # projects to 525,600 rows and 0.388 GiB per install per year of records that BY
    # CONSTRUCTION can never be delivered. Lite is the free tier whose promise is that it
    # just runs.
    #
    # The counter-argument, put to me explicitly and answered rather than waved away: a
    # Lite user who later joins a fleet would arrive with history. They would not arrive
    # with anything WORTH having. This queue is a transport buffer, not a source of truth
    # - the same usage is already in `claude_usage.db` and `~/.usage-tracker/activity.db`,
    # which is where a backfill would read from, and it would read one authoritative row
    # per day rather than 1440 per-cycle snapshots of which 1439 are superseded. Worse,
    # records buffered before the install had an identity carry `unknown` forever
    # (`install_identity.stamp()` is resolved at enqueue), so a year of accumulated history
    # would arrive largely unattributable. Buffering while unconfigured buys a worse
    # backfill than not buffering at all.
    #
    # Membership is decided by the ENDPOINT, not by the enable flag: an install that has a
    # receiver configured but push explicitly paused is still a fleet member with something
    # to deliver, so it keeps buffering. `enqueue()` itself is unconditional; the policy
    # lives here, at the collector boundary, because this is where "should this install be
    # buffering at all" is the question being asked.
    if endpoint() is None:
        return {
            **push_state(),
            "reason": REASON_NOT_A_FLEET_MEMBER,
            "buffered": False,
            "record_id": None,
            "sent": 0,
            "acknowledged": 0,
            "discarded": 0,
        }

    try:
        discarded_before = counts()["discarded_total"]
        record_id = enqueue(readings, timestamp=timestamp)
        discarded = counts()["discarded_total"] - discarded_before
    except Exception as exc:  # noqa: BLE001 - a broken buffer must not break collection
        logger.warning("could not buffer this cycle for fleet push: %s", exc)
        return {"state": STATE_READY, "reason": f"buffer_failed: {exc}", "record_id": None,
                "buffered": False, "discarded": 0, "sent": 0, "acknowledged": 0}
    try:
        result = flush(limit=limit)
    except Exception as exc:  # noqa: BLE001 - same, for the push half
        logger.warning("fleet push failed: %s", exc)
        return {"state": STATE_READY, "reason": f"push_failed: {exc}", "record_id": record_id,
                "buffered": record_id is not None, "discarded": discarded,
                "sent": 0, "acknowledged": 0}
    # A discard outranks whatever else happened this cycle in the reason line: losing
    # buffered records is the thing an operator most needs to see, and a successful push
    # in the same cycle must not hide it.
    reason = REASON_BACKLOG_CAPPED if discarded else result["reason"]
    return {**result, "reason": reason, "record_id": record_id,
            "buffered": record_id is not None, "discarded": discarded}
