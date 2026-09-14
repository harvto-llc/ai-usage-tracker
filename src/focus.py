"""FOCUS 1.3 fact rows derived from local usage, for invoice-grade cost reporting.

FOCUS is the FinOps Open Cost and Usage Specification. Its value here is that a
subscription and the usage it covers stop being the same number: a monthly plan is a
Purchase row carrying what you actually pay, and the tokens it covers are Usage rows
carrying what you consumed. That separation is what makes invoice reconciliation and
interoperability with FinOps tools possible.

This module implements the design in the full tracker's
`specs/feature-3-finops-data-model.md`: the table and its emission, EffectiveCost
amortisation, and FOCUS-named CSV export. Emission writes a Usage row with
effective_cost NULL, because the subscription residual needs the whole month in view;
`amortise_period()` fills both sides in one pass, and `summary_for_period()` reads the
result back as a reconciliation against the plan's monthly cost.

The month is billed ONCE. The upstream design shows a Purchase row carrying the full
monthly cost alongside Usage rows carrying an amortised share, which taken literally
counts the subscription twice. Here the Usage rows carry the share that consumption
used and the Purchase row carries the residual - the capacity paid for and not
consumed - so that, for a provider-month:

    sum(Usage.effective_cost) + Purchase.effective_cost == the plan's monthly cost

NOTHING IS INVENTED. A Purchase row is emitted only when the plan is actually known,
because a subscription cost is a fact about the user's billing rather than something
to infer from a plan name. When the plan is unknown the row is not emitted at all and
the caller is told, which is the same discipline the ROI ratios follow: an unmeasured
cost is recorded as absent, never as 0.00, because a zero reads as "this was free".
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from src import entitlements, install_identity, plan_config, usage_ledger

# The FOCUS release this module targets. The FOCUS Validator supports 1.3 conformance
# testing today; running it is a capability, not a certification, and nothing here
# claims conformance that has not been validated.
FOCUS_VERSION = "1.3"

CHARGE_CATEGORIES = frozenset({"Purchase", "Usage", "Credit", "Tax"})

# Where a row came from. A number is only as good as its origin, so the origin travels
# with the row: a reader must be able to tell a figure this machine DERIVED from local
# telemetry apart from one a vendor actually BILLED. Without that, an invoice-derived
# row and a telemetry-derived row for the same provider-month are indistinguishable,
# and comparing them - the whole point of ingesting invoices - is impossible.
SOURCE_TELEMETRY = "telemetry"
SOURCE_INVOICE = "invoice"
SOURCE_CUSTOMER_EXPORT = "customer_export"
SOURCES = frozenset({SOURCE_TELEMETRY, SOURCE_INVOICE, SOURCE_CUSTOMER_EXPORT})

# Weeks in a month, the divisor the upstream formula uses to turn a monthly
# subscription into the weekly cost that a weekly cap is measured against:
# EffectiveCost = (ConsumedQuantity / weekly_cap) * (monthly_cost / WEEKS_PER_MONTH).
# It is a fixed convention rather than the real length of the month, so that every
# provider-month amortises against the same denominator the caps are written in.
WEEKS_PER_MONTH = 4.33

# What `emit_usage_rows` writes into x_cost_basis before anything has been settled, and
# what `amortise_period` always overwrites. Named rather than repeated as a literal
# because `providers_awaiting_amortisation` decides whether a month was ever settled by
# comparing against it, and a scheduler running off a drifted copy of this string would
# find nothing and silently finalise nothing.
BASIS_NOT_COMPUTED = "not_computed"

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def _db_path() -> Path:
    configured = os.environ.get("USAGE_TRACKER_ACTIVITY_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".usage-tracker" / "activity.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    """Create the focus_facts table if absent. Safe to call repeatedly."""
    usage_ledger.init()
    path_key = str(_db_path())
    if path_key in _initialized_paths:
        return
    with _init_lock:
        if path_key in _initialized_paths:
            return
        with closing(_connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS focus_facts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_provider_name TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    charge_category TEXT NOT NULL,
                    charge_period_start TEXT NOT NULL,
                    charge_period_end TEXT NOT NULL,
                    billing_period_start TEXT NOT NULL,
                    billing_period_end TEXT NOT NULL,
                    pricing_quantity REAL,
                    pricing_unit TEXT,
                    consumed_quantity REAL,
                    consumed_unit TEXT,
                    billed_cost REAL,
                    effective_cost REAL,
                    billing_currency TEXT NOT NULL DEFAULT 'USD',
                    x_agent_vendor TEXT,
                    x_agent_product TEXT,
                    x_model TEXT,
                    x_prompt_tokens INTEGER,
                    x_output_tokens INTEGER,
                    x_cached_tokens INTEGER,
                    x_reasoning_tokens INTEGER,
                    x_messages INTEGER,
                    x_requests INTEGER,
                    x_billing_period_basis TEXT,
                    x_cost_basis TEXT,
                    x_source TEXT,
                    x_install_id TEXT NOT NULL DEFAULT 'unknown',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_focus_facts_period
                ON focus_facts(billing_period_start, service_provider_name);
                """
            )
            # A table created before x_reasoning_tokens existed gets the column added
            # rather than left behind, so a reader cannot get a row whose reasoning
            # tokens are missing purely because of when the database was first made.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(focus_facts)")}
            if "x_reasoning_tokens" not in existing:
                conn.execute("ALTER TABLE focus_facts ADD COLUMN x_reasoning_tokens INTEGER")
            # Which install produced the row. `NOT NULL DEFAULT 'unknown'` fills every
            # row already in the table in the one ALTER, so nothing is left
            # NULL-by-accident. The default is NOT this install's id: the rows predate
            # the column, so which install wrote them was never observed, and writing a
            # real-looking id onto them would be a claim rather than a fact. It is also
            # deliberately NOT added to `idx_focus_facts_identity_v2` - see
            # `_migrate_to_source_identity` for why that would be a regression.
            if "x_install_id" not in existing:
                conn.execute(
                    "ALTER TABLE focus_facts ADD COLUMN x_install_id TEXT NOT NULL "
                    f"DEFAULT '{install_identity.UNKNOWN_INSTALL_ID}'"
                )
            _migrate_to_source_identity(conn, existing)
            conn.commit()
        _initialized_paths.add(path_key)


_IDENTITY_INDEX_NAME = "idx_focus_facts_identity_v2"
_IDENTITY_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_IDENTITY_INDEX_NAME} ON focus_facts("
    "service_provider_name, charge_category, charge_period_start, "
    "COALESCE(x_model, ''), COALESCE(x_source, 'telemetry'))"
)


def _normalised_sql(text: str) -> str:
    """Collapse whitespace so a stored index definition compares by content, not layout.

    SQLite stores the CREATE statement as it was written, newlines and all, so comparing
    the raw text would rebuild the index on every init() purely because someone had once
    typed it across more lines.
    """
    return " ".join((text or "").split())


def _migrate_to_source_identity(conn: sqlite3.Connection, existing: set[str]) -> None:
    """Widen the row identity to include `x_source`, on a table that already holds rows.

    Three properties this has to have, none of which a plain `CREATE UNIQUE INDEX IF
    NOT EXISTS` would give:

    - **No row is lost.** Adding a nullable column and swapping an index never rewrites
      the table, so every row a user already had survives.
    - **Every existing row gets a DEFINED source, not NULL-by-accident.** Any row that
      can predate this column was written by `emit_usage_rows` or `emit_purchase_row`,
      and both derive from local telemetry, so `telemetry` is a fact about those rows
      rather than a default chosen for convenience.
    - **A NEW INDEX NAME.** `CREATE UNIQUE INDEX IF NOT EXISTS idx_focus_facts_identity`
      against a database that already carries that name is a silent NO-OP: existing
      databases would keep the OLD narrow identity while freshly created ones got the
      new one, and no test that runs on a fresh database could ever see the difference.
      Dropping the old name and creating a differently named index fails closed instead.

    `COALESCE(x_source, 'telemetry')` rather than `COALESCE(x_source, '')` so that a row
    with no source keeps EXACTLY the identity it held before the migration; the widened
    index therefore cannot admit a duplicate the narrow one refused.

    Reversing it: delete the non-telemetry rows, drop `idx_focus_facts_identity_v2`, and
    recreate `idx_focus_facts_identity` with the original four-part identity. No
    telemetry row is lost by that, and the `x_source` column can stay - it is the index,
    not the column, that changes behaviour.

    WHY `x_install_id` IS NOT PART OF THIS IDENTITY, and why adding it would be a
    regression rather than a widening. `x_source` was added to the index because two
    rows for the same provider-month genuinely ARE different facts when one came from a
    vendor invoice and the other from local telemetry. `x_install_id` is not like that.
    This database has no sync, merge or import path - `activity.db` is plain sqlite3 and
    neither `focus.py`, `usage_ledger.py` nor `work_ledger.py` mentions libsql or Turso -
    so every row in one file was produced by one install. What the column WOULD do inside
    the index is split identity across the upgrade itself: a fact written before the
    column existed carries `unknown` and the same fact written afterwards carries the
    real id, so a unique index over it would treat them as two identities and admit BOTH
    where today it refuses the second. That matters for the index in its role as a
    BACKSTOP - `emit_usage_rows` deletes the provider-day's telemetry rows before
    re-emitting, so ordinary re-emission never reaches the index at all; what the index
    catches is a second writer producing the same fact. If some later feature ever merges
    two installs' facts into one `activity.db`, that feature has to extend this function
    to a v3 index under a NEW name, for exactly the reason spelled out above.
    """
    if "x_source" not in existing:
        conn.execute("ALTER TABLE focus_facts ADD COLUMN x_source TEXT")
    conn.execute(
        "UPDATE focus_facts SET x_source = ? WHERE x_source IS NULL", (SOURCE_TELEMETRY,)
    )
    conn.execute("DROP INDEX IF EXISTS idx_focus_facts_identity")
    # The v2 definition is VERIFIED, not assumed. `IF NOT EXISTS` matches on the name
    # alone, so a v2 left behind by a different definition - an interrupted upgrade, a
    # future revision, a hand-edited database - would silently keep its own identity
    # while this code believed it had installed one. Read what is actually there and
    # rebuild it when it disagrees; repeated init() is still a no-op when it agrees.
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (_IDENTITY_INDEX_NAME,),
    ).fetchone()
    if stored is not None and _normalised_sql(stored["sql"]) != _normalised_sql(_IDENTITY_INDEX_SQL):
        conn.execute(f"DROP INDEX {_IDENTITY_INDEX_NAME}")
        stored = None
    if stored is None:
        conn.execute(_IDENTITY_INDEX_SQL)


def _checked_source(source: str) -> str:
    """Reject an unknown source at the point of writing it, the way categories are.

    Fails closed for the same reason `_checked_category` does: a row whose provenance is
    a string nobody defined is worse than no row, because every reader downstream would
    have to guess whether it was measured or billed.
    """
    if source not in SOURCES:
        raise ValueError(
            f"x_source {source!r} is not a known source; expected one of {sorted(SOURCES)}"
        )
    return source


def _checked_category(category: str) -> str:
    """Reject a charge category FOCUS does not define, at the point of writing it.

    CHARGE_CATEGORIES is the constraint, not decoration. It cannot be a CHECK on the
    table without rebuilding a table that already exists in users' databases, so it is
    enforced here instead, and it fails closed: an unrecognised category raises rather
    than landing a row that every downstream reader would have to special-case.
    """
    if category not in CHARGE_CATEGORIES:
        raise ValueError(
            f"charge_category {category!r} is not a FOCUS {FOCUS_VERSION} category; "
            f"expected one of {sorted(CHARGE_CATEGORIES)}"
        )
    return category


def _now_us() -> int:
    import time

    return time.time_ns() // 1000


def _resolved_plan(provider: str, plans: dict | None) -> dict:
    """Resolve a plan the way every other call site in this repo does.

    resolve_plan derives everything from detected_plan or plans; called with the
    provider alone it returns known_plan False for EVERY provider, which silently
    disables plan-dependent behaviour instead of failing loudly.
    """
    return entitlements.resolve_plan(provider, plans=plans if plans is not None else plan_config.load_plans())


def _configured_monthly_cost(provider: str, plans: dict | None) -> float | None:
    """The user's own monthly cost for a provider, or None when they never set one.

    Read from plans.toml `cost_usd_month`, which is what the rest of the repo treats
    as real spend (plan_config.total_monthly_cost, metrics). The plan catalog's
    monthly_usd is a hardcoded LIST price, and writing a list price into a table whose
    purpose is invoice reconciliation would be inventing the very number the row
    exists to reconcile. A user on an annual or discounted plan pays something else.
    """
    plans = plans if plans is not None else plan_config.load_plans()
    config = plans.get(provider)
    if not isinstance(config, dict):
        return None
    raw = config.get("cost_usd_month")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _weekly_cap_tokens(provider: str, plans: dict | None) -> float | None:
    """The user's own weekly token cap for a provider, or None when unconfigured.

    Read from plans.toml `[<provider>.self_quota] weekly_cap_tokens`, the same cap the
    self-quota tracking already treats as this user's real ceiling. There is no
    fallback on purpose: the entitlements catalog carries no token cap at all, only
    relative multipliers like session_capacity_vs_pro, so a substitute denominator
    would be invented and every amortised number downstream of it would be a guess.

    A zero or negative cap is treated as absent rather than divided by.
    """
    plans = plans if plans is not None else plan_config.load_plans()
    config = plan_config.self_quota_config(provider, plans)
    if not config:
        return None
    raw = config.get("weekly_cap_tokens")
    if raw is None:
        return None
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def _month_bounds(day: str) -> tuple[str, str]:
    """Calendar-month bounds for a YYYY-MM-DD day.

    These are CALENDAR months, not the user's billing anniversary, which the local
    data does not know. Rows record that basis in x_billing_period_basis so a reader
    never mistakes them for invoice-aligned periods.

    An impossible month is rejected rather than propagated: a period of "2026-13-01"
    is unreachable by any real-month query, so it would remove the cost from every
    report silently.
    """
    year, month, _ = (int(part) for part in day.split("-"))
    if not 1 <= month <= 12:
        raise ValueError(f"day has an impossible month: {day!r}")
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _next_day(day: str) -> str:
    from datetime import date, timedelta

    year, month, dom = (int(part) for part in day.split("-"))
    return (date(year, month, dom) + timedelta(days=1)).isoformat()


def emit_usage_rows(
    provider: str, day: str, *, plans: dict | None = None, now_us: int | None = None
) -> int:
    """Emit one Usage row per model for a provider-day. Returns rows written.

    Idempotent: re-emitting the same provider-day replaces that day's rows rather
    than appending duplicates, so a daily job may run more than once.

    effective_cost is left NULL here and filled by `amortise_period()`. A day's share
    of a subscription cannot be settled from that day alone: the Purchase residual it
    has to agree with is a property of the whole month.
    """
    init()
    billing_start, billing_end = _month_bounds(day)
    charge_end = _next_day(day)
    rows = usage_ledger.daily_models(provider, day, day)
    plan = _resolved_plan(provider, plans)
    service_name = plan.get("plan_label") or provider
    stamp = _now_us() if now_us is None else int(now_us)
    install = install_identity.stamp()

    written = 0
    with closing(_connect()) as conn:
        # Deleted before the empty check, so emission makes the day MATCH upstream
        # rather than only replacing a non-empty day. If upstream usage for this day
        # is corrected away to nothing, the previously emitted rows must go too;
        # otherwise the table keeps reporting consumption nobody believes happened.
        # Scoped to telemetry. The predicate names neither x_model nor the source, so
        # without this clause a telemetry re-emission would delete an INVOICE row for the
        # same provider-day: it is this DELETE, not the unique index, that destroys the
        # other supply route's rows, and widening the index alone would not stop it.
        conn.execute(
            """DELETE FROM focus_facts
               WHERE service_provider_name = ? AND charge_category = 'Usage'
                 AND charge_period_start = ?
                 AND COALESCE(x_source, ?) = ?""",
            (provider, day, SOURCE_TELEMETRY, SOURCE_TELEMETRY),
        )
        for row in rows:
            cached = (
                int(row["cache_read_tokens"])
                + int(row["cache_write_5m_tokens"])
                + int(row["cache_write_1h_tokens"])
            )
            consumed = (
                int(row["input_tokens"]) + int(row["output_tokens"]) + cached
            )
            # Recorded beside the totals, deliberately NOT added into consumed: a
            # provider reports reasoning tokens as part of the output tokens it already
            # billed, so adding them would inflate the quantity that phase 2 divides by
            # the cap. A row whose upstream never reported them keeps NULL, not 0.
            raw_reasoning = row.get("reasoning_tokens")
            reasoning = None if raw_reasoning is None else int(raw_reasoning)
            conn.execute(
                """INSERT INTO focus_facts(
                       service_provider_name, service_name, charge_category,
                       charge_period_start, charge_period_end,
                       billing_period_start, billing_period_end,
                       pricing_quantity, pricing_unit,
                       consumed_quantity, consumed_unit,
                       billed_cost, effective_cost, billing_currency,
                       x_agent_vendor, x_agent_product, x_model,
                       x_prompt_tokens, x_output_tokens, x_cached_tokens,
                       x_reasoning_tokens, x_messages, x_requests,
                       x_billing_period_basis, x_cost_basis, x_source, x_install_id,
                       created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    provider, service_name, _checked_category("Usage"),
                    day, charge_end,
                    billing_start, billing_end,
                    None, None,
                    float(consumed), "tokens",
                    None, None, "USD",
                    provider, plan.get("plan_label"), row["model"],
                    int(row["input_tokens"]), int(row["output_tokens"]), cached,
                    reasoning, int(row["messages"]), int(row["requests"]),
                    "calendar_month", BASIS_NOT_COMPUTED, _checked_source(SOURCE_TELEMETRY),
                    install,
                    stamp,
                ),
            )
            written += 1
        conn.commit()
    return written


def emit_purchase_row(
    provider: str, day: str, *, plans: dict | None = None, now_us: int | None = None
) -> bool:
    """Emit the monthly Purchase row for a provider, if the plan cost is known.

    Returns False without writing when the plan is unknown or carries no monthly
    cost. A subscription figure is a fact about the user's billing; inferring one
    would put an invented number into a table whose purpose is invoice reconciliation.

    effective_cost is left NULL here and filled by `amortise_period()`, which sets it
    to the residual the month's Usage rows did not take.

    KNOWN LIMITATION, deliberately not fixed here: the row's identity is
    provider + Purchase + month, so a mid-month plan change overwrites the earlier
    plan's row and charges the new price for the whole month, with no record that the
    month was split. Recording it truthfully needs day-level pro-rating of two plans,
    which this table's monthly Purchase grain cannot express, so the limitation is
    documented rather than half-implemented.
    """
    init()
    plan = _resolved_plan(provider, plans)
    monthly = _configured_monthly_cost(provider, plans)
    if monthly is None:
        return False

    billing_start, billing_end = _month_bounds(day)
    stamp = _now_us() if now_us is None else int(now_us)
    install = install_identity.stamp()
    with closing(_connect()) as conn:
        # Scoped to telemetry for the same reason the Usage delete is: an invoice-derived
        # Purchase row for this provider-month is a different fact, not a stale copy.
        conn.execute(
            """DELETE FROM focus_facts
               WHERE service_provider_name = ? AND charge_category = 'Purchase'
                 AND charge_period_start = ?
                 AND COALESCE(x_source, ?) = ?""",
            (provider, billing_start, SOURCE_TELEMETRY, SOURCE_TELEMETRY),
        )
        conn.execute(
            """INSERT INTO focus_facts(
                   service_provider_name, service_name, charge_category,
                   charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end,
                   pricing_quantity, pricing_unit,
                   consumed_quantity, consumed_unit,
                   billed_cost, effective_cost, billing_currency,
                   x_agent_vendor, x_agent_product, x_model,
                   x_prompt_tokens, x_output_tokens, x_cached_tokens,
                   x_reasoning_tokens, x_messages, x_requests,
                   x_billing_period_basis, x_cost_basis, x_source, x_install_id,
                   created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                provider, plan.get("plan_label") or provider, _checked_category("Purchase"),
                billing_start, billing_end,
                billing_start, billing_end,
                1.0, "seat-month",
                1.0, "seat-month",
                float(monthly), None, "USD",
                provider, plan.get("plan_label"), None,
                None, None, None,
                None, None, None,
                "calendar_month", "plans_toml:cost_usd_month",
                _checked_source(SOURCE_TELEMETRY), install, stamp,
            ),
        )
        conn.commit()
    return True


def amortise_period(
    provider: str, billing_period_start: str, *, plans: dict | None = None
) -> dict:
    """Spread a provider's monthly subscription across the usage it covered.

    Rewrites effective_cost for one provider-month in a single pass, both sides at
    once, because the two sides are one division of a single bill:

    - each Usage row takes the upstream share, `(consumed / weekly_cap) *
      (monthly_cost / WEEKS_PER_MONTH)`;
    - the Purchase row takes the residual, `monthly_cost - sum(Usage shares)`, which
      is the capacity that was paid for and not consumed.

    So the month totals the bill exactly once. Re-running is idempotent: every row is
    recomputed from its consumption, never incremented.

    Where a denominator is missing the cost is recorded as ABSENT, never as 0.00 - a
    zero here would read as "this usage was free". With no plan cost there is nothing
    to divide, so both sides go NULL, including a Purchase row left behind by an
    earlier configuration; with no cap the bill is real but unallocatable, so it stays
    whole on the Purchase row and the Usage rows say why they carry nothing.
    x_cost_basis names the reason in every case.

    Scoped to TELEMETRY-derived rows on both sides. Amortisation divides the user's own
    plans.toml cost across the consumption this machine observed; an invoice-derived row
    carries a figure a vendor actually billed, and rewriting that with a derivation would
    replace a measured number with a computed one in the one table whose purpose is to
    reconcile the two against each other.

    Returns a summary of what was decided, including `balanced`, which is True only
    when the month actually reconciles against the plan cost.
    """
    init()
    plans = plans if plans is not None else plan_config.load_plans()
    monthly = _configured_monthly_cost(provider, plans)
    cap = _weekly_cap_tokens(provider, plans)

    if monthly is None:
        absent_basis = "plan_cost_unknown:no_plans_toml_cost_usd_month"
    elif cap is None:
        absent_basis = "cap_unknown:no_plans_toml_self_quota_weekly_cap_tokens"
    else:
        absent_basis = None

    update = """UPDATE focus_facts SET effective_cost = ?, x_cost_basis = ?
                WHERE id = ?"""

    with closing(_connect()) as conn:
        usage_rows = conn.execute(
            """SELECT id, consumed_quantity FROM focus_facts
               WHERE service_provider_name = ? AND charge_category = 'Usage'
                 AND billing_period_start = ?
                 AND COALESCE(x_source, ?) = ?""",
            (provider, billing_period_start, SOURCE_TELEMETRY, SOURCE_TELEMETRY),
        ).fetchall()

        amortised_total = 0.0
        amortised_rows = 0
        unamortised_rows = 0
        for row in usage_rows:
            consumed = row["consumed_quantity"]
            if absent_basis is not None:
                conn.execute(update, (None, absent_basis, row["id"]))
                unamortised_rows += 1
                continue
            if consumed is None:
                # A row with no consumption cannot take a share, and its absence makes
                # the residual unreliable too; both stay NULL rather than guessing.
                conn.execute(update, (None, "consumption_unknown", row["id"]))
                unamortised_rows += 1
                continue
            share = (float(consumed) / cap) * (monthly / WEEKS_PER_MONTH)
            amortised_total += share
            conn.execute(
                update,
                (share, "amortised:consumed/weekly_cap*monthly_cost/weeks_per_month", row["id"]),
            )
            amortised_rows += 1

        purchase_effective: float | None = None
        purchase_basis: str | None = None
        purchase_updated = 0
        if monthly is None:
            # A Purchase row emitted under an earlier configuration outlives the cost
            # that justified it, because emission withholds a row rather than deleting
            # one. Its residual must not: a figure derived from a plan cost this user
            # no longer declares is exactly the invented number this table exists to
            # keep out, and summary_for_period would total it as real money.
            purchase_basis = absent_basis
        elif cap is None:
            # The bill is known and none of it could be allocated, so all of it stays
            # here. Still counted once: every Usage row carries NULL.
            purchase_effective = monthly
            purchase_basis = "unallocated_subscription:cap_unknown"
        elif unamortised_rows:
            purchase_effective = None
            purchase_basis = "residual_indeterminate:unamortised_usage_rows"
        else:
            purchase_effective = monthly - amortised_total
            if purchase_effective < 0:
                # Consumption exceeded the configured cap, so the cap understates what
                # the plan really allows. Recorded as the negative it is rather than
                # clamped to zero, which would silently drop the excess out of the
                # month's total.
                purchase_basis = "residual_negative:consumption_exceeded_configured_weekly_cap"
            else:
                purchase_basis = "residual_unused_capacity:plan_cost_minus_amortised_usage"
        cursor = conn.execute(
            """UPDATE focus_facts SET effective_cost = ?, x_cost_basis = ?
               WHERE service_provider_name = ? AND charge_category = 'Purchase'
                 AND billing_period_start = ?
                 AND COALESCE(x_source, ?) = ?""",
            (purchase_effective, purchase_basis, provider, billing_period_start,
             SOURCE_TELEMETRY, SOURCE_TELEMETRY),
        )
        purchase_updated = cursor.rowcount
        conn.commit()

    balanced = (
        monthly is not None
        and purchase_updated == 1
        and purchase_effective is not None
        and abs((amortised_total + purchase_effective) - monthly) <= 1e-9
    )
    return {
        "provider": provider,
        "billing_period_start": billing_period_start,
        "monthly_cost": monthly,
        "weekly_cap_tokens": cap,
        "usage_rows": len(usage_rows),
        "amortised_rows": amortised_rows,
        "unamortised_rows": unamortised_rows,
        "amortised_usage_total": amortised_total,
        "purchase_rows": purchase_updated,
        "purchase_effective_cost": purchase_effective,
        "purchase_cost_basis": purchase_basis,
        "balanced": balanced,
    }


# FOCUS 1.3 column names, in export order, mapped from this table's snake_case columns.
# The standard columns come first in the specification's own order, then the x_ columns
# this repo defines. `id` and `created_at` are deliberately absent: they are local
# bookkeeping, not FOCUS columns, and exporting them would imply a conformance this
# module does not claim.
FOCUS_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("service_provider_name", "ServiceProviderName"),
    ("service_name", "ServiceName"),
    ("charge_category", "ChargeCategory"),
    ("charge_period_start", "ChargePeriodStart"),
    ("charge_period_end", "ChargePeriodEnd"),
    ("billing_period_start", "BillingPeriodStart"),
    ("billing_period_end", "BillingPeriodEnd"),
    ("pricing_quantity", "PricingQuantity"),
    ("pricing_unit", "PricingUnit"),
    ("consumed_quantity", "ConsumedQuantity"),
    ("consumed_unit", "ConsumedUnit"),
    ("billed_cost", "BilledCost"),
    ("effective_cost", "EffectiveCost"),
    ("billing_currency", "BillingCurrency"),
    ("x_agent_vendor", "x_AgentVendor"),
    ("x_agent_product", "x_AgentProduct"),
    ("x_model", "x_Model"),
    ("x_prompt_tokens", "x_PromptTokens"),
    ("x_output_tokens", "x_OutputTokens"),
    ("x_cached_tokens", "x_CachedTokens"),
    ("x_reasoning_tokens", "x_ReasoningTokens"),
    ("x_messages", "x_Messages"),
    ("x_requests", "x_Requests"),
    ("x_billing_period_basis", "x_BillingPeriodBasis"),
    ("x_cost_basis", "x_CostBasis"),
    # Provenance travels with the number even out of the process: an exported row that
    # did not say where it came from would let a spreadsheet add a vendor's billed figure
    # to this machine's derived one as though both were the same kind of fact.
    ("x_source", "x_Source"),
)


def period_start(period: str) -> str:
    """Turn a YYYY-MM period into its billing_period_start, rejecting anything else.

    Rejected rather than coerced: a period that silently became something else would
    return a confidently empty export, which reads as "you spent nothing that month".
    """
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-\d{2}", period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    month = int(period[5:7])
    if not 1 <= month <= 12:
        raise ValueError(f"period has an impossible month: {period!r}")
    return f"{period}-01"


def export_focus_csv(period: str) -> str:
    """Render a YYYY-MM period as FOCUS 1.3 CSV text.

    An absent cost is an EMPTY field, never 0. A spreadsheet or a FinOps importer sums
    a column; a zero would be summed as "this was free", while an empty cell is the
    unknown it actually is. That is the same rule the rows themselves follow.
    """
    rows = facts_for_period(period_start(period))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([focus_name for _, focus_name in FOCUS_CSV_COLUMNS])
    for row in rows:
        # csv writes None as an empty field, which is exactly the intent here.
        writer.writerow([row.get(column) for column, _ in FOCUS_CSV_COLUMNS])
    return buffer.getvalue()


def summary_for_period(period: str, *, plans: dict | None = None) -> dict:
    """Per-provider cost totals for a period, with what is missing made visible.

    A total is a sum of the rows that carry a cost. Rows that do not carry one are
    COUNTED, not treated as zero, and a provider whose rows all lack a cost totals
    None rather than 0.00. `reconciles` is the phase-2 invariant read back off the
    stored rows: the effective costs of a provider-month add up to the plan's monthly
    cost exactly once, or they do not and the caller can see it.

    EVERY MEASURED FIGURE IN THE PROVIDER BUCKET IS TELEMETRY-DERIVED, and
    `totals_source` says so in the payload rather than leaving a reader to assume it.
    That covers `consumed_by_unit` as much as the money: an invoice line that states a
    quantity - one seat-month, say - would otherwise be added to the seat-month the
    telemetry Purchase row already recorded, and the provider would appear to hold two
    seats. Blending the money is the same trap one level up: the month would be counted
    once as this machine derived it and again as the vendor billed it, and `reconciles`
    would flip false for a month that reconciles perfectly well.

    So every source's figures are reported in `by_source`, which DECOMPOSES the provider
    instead of blending it, and no existing figure changes value when a second source
    lands. The one field whose value does move is `rows`, which counts the rows that
    exist for the provider and therefore grows - deliberately, because a row count that
    hid rows would be a worse lie than a count that includes them. `telemetry_rows` is
    carried beside it so that any ratio taken against the telemetry figures has a
    denominator drawn from exactly the rows those figures came from.
    """
    billing_start = period_start(period)
    rows = facts_for_period(billing_start)
    plans = plans if plans is not None else plan_config.load_plans()

    providers: dict[str, dict] = {}
    for row in rows:
        name = row["service_provider_name"]
        bucket = providers.setdefault(
            name,
            {
                "service_provider_name": name,
                "rows": 0,
                "telemetry_rows": 0,
                "totals_source": SOURCE_TELEMETRY,
                "billed_cost_total": None,
                "effective_cost_total": None,
                "rows_without_effective_cost": 0,
                "by_source": {},
                # Kept per unit and never added together. A Purchase row consumes 1
                # seat-month and a Usage row consumes tokens; one total across both
                # would be a number with no unit, and 1701 would read as tokens.
                "consumed_by_unit": {},
                "monthly_plan_cost": _configured_monthly_cost(name, plans),
                "reconciles": False,
            },
        )
        bucket["rows"] += 1
        # None-only, never truthiness. The index reads provenance as
        # COALESCE(x_source,'telemetry'), which maps NULL to telemetry and leaves an
        # EMPTY STRING as its own identity. `or` would coerce '' as well, so a row whose
        # provenance is unknown would be reported as one this machine MEASURED.
        raw_source = row.get("x_source")
        source = SOURCE_TELEMETRY if raw_source is None else raw_source
        per_source = bucket["by_source"].setdefault(
            source,
            {
                "x_source": source,
                "rows": 0,
                "billed_cost_total": None,
                "effective_cost_total": None,
                "rows_without_effective_cost": 0,
                "consumed_by_unit": {},
            },
        )
        per_source["rows"] += 1
        for column, key in (("billed_cost", "billed_cost_total"),
                            ("effective_cost", "effective_cost_total")):
            value = row[column]
            if value is None:
                continue
            per_source[key] = (per_source[key] or 0.0) + float(value)
            if source == SOURCE_TELEMETRY:
                bucket[key] = (bucket[key] or 0.0) + float(value)
        if row["effective_cost"] is None:
            per_source["rows_without_effective_cost"] += 1
        if source == SOURCE_TELEMETRY:
            bucket["telemetry_rows"] += 1
            if row["effective_cost"] is None:
                bucket["rows_without_effective_cost"] += 1
        if row["consumed_quantity"] is not None:
            unit = row["consumed_unit"] or "unknown"
            quantity = float(row["consumed_quantity"])
            per_unit = per_source["consumed_by_unit"]
            per_unit[unit] = per_unit.get(unit, 0.0) + quantity
            if source == SOURCE_TELEMETRY:
                by_unit = bucket["consumed_by_unit"]
                by_unit[unit] = by_unit.get(unit, 0.0) + quantity

    for bucket in providers.values():
        plan_cost = bucket["monthly_plan_cost"]
        total = bucket["effective_cost_total"]
        bucket["reconciles"] = (
            plan_cost is not None
            and total is not None
            and abs(total - plan_cost) <= 1e-9
        )

    for bucket in providers.values():
        bucket["by_source"] = [
            bucket["by_source"][name] for name in sorted(bucket["by_source"])
        ]

    return {
        "focus_version": FOCUS_VERSION,
        "period": period,
        "billing_period_start": billing_start,
        "rows": len(rows),
        "providers": [providers[name] for name in sorted(providers)],
    }


def facts_for_period(billing_period_start: str) -> list[dict]:
    """Read emitted rows for a calendar month, newest identity order."""
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT * FROM focus_facts
               WHERE billing_period_start = ?
               ORDER BY charge_category, service_provider_name,
                        charge_period_start, COALESCE(x_model, ''),
                        COALESCE(x_source, 'telemetry')""",
            (billing_period_start,),
        ).fetchall()
    return [dict(row) for row in rows]


def providers_awaiting_amortisation(billing_period_start: str) -> list[str]:
    """Providers whose Usage rows in ONE month were emitted and never amortised.

    Asks the data whether a month was settled instead of trusting a clock. A scheduler
    that finalised the previous month only on the calendar's first day would skip the
    month entirely whenever the machine was asleep that day, and would have no way to
    notice; this returns the same answer on day 1 and on day 9.

    Bounded to one `billing_period_start` on purpose, which `idx_focus_facts_period`
    serves as an equality lookup, so a caller may run it every cycle. Telemetry-scoped
    like the emission and amortisation it pairs with: an invoice-derived row is a fact a
    vendor billed and is not this derivation's to settle.
    """
    init()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT service_provider_name FROM focus_facts
               WHERE billing_period_start = ? AND charge_category = 'Usage'
                 AND COALESCE(x_source, ?) = ?
                 AND x_cost_basis = ?
               ORDER BY service_provider_name""",
            (billing_period_start, SOURCE_TELEMETRY, SOURCE_TELEMETRY, BASIS_NOT_COMPUTED),
        ).fetchall()
    return [row["service_provider_name"] for row in rows]
