"""The driveshaft: what makes FOCUS emission actually run.

`src/focus.py` is the ENGINE. It knows how to turn a provider-day into Usage rows, a
provider-month into a Purchase row, and a month's consumption into amortised
EffectiveCost. It has known how to do that since it was merged, and until this module
existed nothing in the product ever called it: the four `/focus/*` routes are read-only,
and `emit_usage_rows` / `emit_purchase_row` / `amortise_period` had no production call
site anywhere. The table stayed at zero rows on every install unless a human ran the
functions by hand. An engine with no driveshaft turns nothing.

This module owns SCHEDULING POLICY and nothing else - which days get emitted, which
months get settled, and when the previous month is finished with. That is a different
concern from emission, with a different way of going wrong, so it lives in a different
file: the engine is reviewed and live, and policy churn should not touch it.

TWO ENTRY POINTS, WITH DELIBERATELY DIFFERENT APPETITES.

`record_cycle()` runs inside `src/collector.py`, every 60 seconds under launchd. It reads
ONE day - today - and settles the current month, plus a bounded previous-month case. It
must never walk history, because at that cadence a full-history pass is 1440 full-history
passes a day to publish a single day's rows.

`backfill()` walks all of history exactly once. It is for a fresh install, and for an
install that upgraded from a version that emitted nothing, where "the recurring job will
catch up eventually" is false: the recurring job only ever looks at today, so without
this the months before the upgrade would stay permanently empty.

WHY EMISSION CANNOT BREAK COLLECTION. Same rule, and the same shape, as
`fleet_push.record_cycle`: every failure in here becomes a returned state carrying a
reason, and none of them becomes an exception in the collector's cycle. Local usage
collection is the product; FOCUS emission is a reporting layer on top of it, and a
reporting layer that can take collection down with it has the dependency backwards. A
skip is announced rather than silent - `STATE_IDLE` says which condition produced it, and
a provider-month that gets no Purchase row is NAMED in `purchase_absent`, never dropped.

NOTHING IS INVENTED, which is inherited rather than re-decided. `emit_purchase_row`
returns False without writing when `plans.toml` carries no `cost_usd_month`, because a
subscription figure is a fact about the user's billing. This module does not paper over
that with a default; it reports the absence and moves on.
"""

from __future__ import annotations

import logging
from datetime import date

from src import focus, usage_ledger

logger = logging.getLogger(__name__)

# What the cycle decided. A state is always returned and always names a condition; there
# is no code path that reports nothing.
STATE_EMITTED = "emitted"
STATE_IDLE = "idle"
STATE_FAILED = "failed"

REASON_NO_USAGE_TODAY = "no_provider_days_today_and_nothing_pending"


def _previous_month_start(day: str) -> str:
    """The billing_period_start of the month before the one `day` falls in."""
    current_start, _ = focus._month_bounds(day)
    year, month, _ = (int(part) for part in current_start.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12-01"
    return f"{year:04d}-{month - 1:02d}-01"


def _previous_day(day: str) -> str:
    from datetime import timedelta

    year, month, dom = (int(part) for part in day.split("-"))
    return (date(year, month, dom) - timedelta(days=1)).isoformat()


def _emit_provider_day(provider: str, day: str, *, plans, now_us) -> tuple[int, bool]:
    """Emit one provider-day's Usage rows and that month's Purchase row.

    Returns (usage rows written, whether a Purchase row was written). The Purchase
    boolean is passed through from `emit_purchase_row` unchanged: False means the plan
    cost is genuinely unknown, and the caller reports that rather than substituting a
    number for it.
    """
    written = focus.emit_usage_rows(provider, day, plans=plans, now_us=now_us)
    purchased = focus.emit_purchase_row(provider, day, plans=plans, now_us=now_us)
    return written, purchased


# Why a month allocated nothing. These are machine causes, in the order
# `amortise_period` decides them, and they are deliberately NOT phrased as "not
# configured": `focus._weekly_cap_tokens` returns None for a cap that is present but
# zero or negative (`src/focus.py:341`, `return cap if cap > 0 else None`), so a
# summary's None means the denominator is UNAVAILABLE, which is not the same claim as
# "the user never set one".
CAUSE_PLAN_COST_UNKNOWN = "plan_cost_unknown"
CAUSE_WEEKLY_CAP_UNAVAILABLE = "weekly_cap_unavailable"
CAUSE_CONSUMPTION_UNKNOWN = "consumption_unknown"


def _unallocated_cause(summary: dict) -> str:
    """Why a month allocated nothing - DERIVED from the summary, never assumed.

    Reading `src/focus.py:589-603`, three different conditions produce
    `amortised_rows == 0`, and the first version of this reporting named only one of
    them because that is the one this install happens to hit. For the other two the line
    would have stated a reason that was not the reason, which is an invented explanation
    in operator-facing output - the exact thing the Purchase row's own discipline exists
    to prevent.

    `is None` throughout, never truthiness. A `cost_usd_month` of 0.0 is a KNOWN cost of
    zero and must not be classified absent; `monthly_cost` is not None there.

    A row with `consumed_quantity` of 0 is NOT in this set - it takes a share of 0.0 and
    counts as amortised - so `consumption_unknown` means genuinely absent consumption.
    """
    if summary["monthly_cost"] is None:
        return CAUSE_PLAN_COST_UNKNOWN
    if summary["weekly_cap_tokens"] is None:
        return CAUSE_WEEKLY_CAP_UNAVAILABLE
    return CAUSE_CONSUMPTION_UNKNOWN


def _record_amortisation(result: dict, provider: str, period: str, summary: dict) -> None:
    """File one `amortise_period` summary under every heading it belongs to.

    `balanced` ALONE IS NOT A REPORT OF SUCCESS, and this is measured rather than
    theoretical. On the install this was validated against, `plans.toml` declares
    `cost_usd_month = 200` for both providers and no `[<provider>.self_quota]
    weekly_cap_tokens`. `amortise_period` therefore takes its cap-unknown branch: all 479
    Usage rows get effective_cost NULL with
    `cap_unknown:no_plans_toml_self_quota_weekly_cap_tokens`, the whole subscription stays
    on the Purchase row as `unallocated_subscription:cap_unknown`, and `balanced` comes
    back TRUE - because 0 + 200 really does equal 200.

    A cycle that reported only `balanced` would print "11 periods amortised" for a table in
    which not one dollar had been attributed to any usage. The month reconciles and the
    allocation did not happen; those are different facts and both get said. The predicate
    is `amortised_rows == 0` beside `usage_rows > 0`, never a truthiness test: 0 is a real
    count here, not an absent one.
    """
    label = f"{provider}@{period}"
    result["amortised_periods"].append(label)
    if not summary["balanced"]:
        result["unbalanced_periods"].append(label)
    if summary["usage_rows"] > 0 and summary["amortised_rows"] == 0:
        # The label keeps the SAME value shape as every other provider-month list in
        # this module. Folding the cause into it - `provider@period:cause` - would have
        # made an identity string that no longer matches the identity strings beside it,
        # so the explanation goes in a parallel mapping instead.
        result["unallocated_periods"].append(label)
        result["unallocated_causes"][label] = _unallocated_cause(summary)


def record_cycle(
    *, today: str | None = None, plans: dict | None = None, now_us: int | None = None
) -> dict:
    """Emit and settle what this cycle is responsible for. Never raises into the caller.

    THE RECURRING WORK IS ONE DAY WIDE. `usage_ledger.providers_for_day(today)` is a
    single equality on `day`, and each `emit_usage_rows(provider, today)` selects that one
    day back out. No cycle reads a day other than today and, on a month boundary,
    yesterday. History is never rescanned here; `backfill()` is where that belongs.

    THE MONTH BOUNDARY IS TWO PROBLEMS, NOT ONE.

    The obvious one is that a new month starts. The one that actually loses data is that
    emitting only today means the previous month's LAST day is never revisited: usage
    that lands upstream for the 31st after midnight on the 1st would never be emitted, and
    the month would be finalised without it. So on a boundary day this re-emits YESTERDAY
    as well, which is one extra day, once a month.

    The second is that a machine which was asleep on the 1st would, on a purely calendar
    trigger, skip the previous month's settlement entirely and never notice. So the
    previous month is instead finalised from what the DATA says: `providers_awaiting_
    amortisation` finds Usage rows still carrying `not_computed`, which is what emission
    writes and amortisation always overwrites. That question returns the same answer on
    the 1st and on the 9th.

    Both are bounded to the previous month by an indexed equality, which is what makes
    them affordable every 60 seconds.

    NAMED RESIDUAL: an outage spanning more than one whole calendar month leaves month
    M-2 unsettled. The sweep is deliberately one month wide for cost, and `backfill()` is
    the stated remedy. This is recorded rather than hidden: the cycle does not claim to
    repair an outage of arbitrary length.
    """
    day = today if today is not None else date.today().isoformat()

    result: dict = {
        "state": STATE_IDLE,
        "reason": REASON_NO_USAGE_TODAY,
        "day": day,
        "providers": [],
        "usage_rows": 0,
        "purchase_rows": 0,
        "purchase_absent": [],
        "amortised_periods": [],
        "unbalanced_periods": [],
        "unallocated_periods": [],
        "unallocated_causes": {},
        "boundary_day": False,
        "failures": [],
    }

    current_start, _ = focus._month_bounds(day)

    # PHASE 1 - today. Wrapped on its own so that a failure here still leaves the
    # previous-month settlement below a chance to run, and vice versa. A single
    # try/except around both would let one broken provider cost the other phase.
    try:
        providers_today = usage_ledger.providers_for_day(day)
        for provider in providers_today:
            written, purchased = _emit_provider_day(provider, day, plans=plans, now_us=now_us)
            result["usage_rows"] += written
            if purchased:
                result["purchase_rows"] += 1
            else:
                result["purchase_absent"].append(f"{provider}@{current_start}")
        for provider in providers_today:
            summary = focus.amortise_period(provider, current_start, plans=plans)
            _record_amortisation(result, provider, current_start, summary)
        result["providers"] = providers_today
    except Exception as exc:  # noqa: BLE001 - emission must not break collection
        logger.warning("focus emission failed for %s: %s", day, exc)
        result["failures"].append(f"today_failed: {exc}")

    # PHASE 2 - the previous month, finished with.
    try:
        previous_start = _previous_month_start(day)
        yesterday = _previous_day(day)
        boundary = focus._month_bounds(yesterday)[0] != current_start
        result["boundary_day"] = boundary

        boundary_providers: list[str] = []
        if boundary:
            # Late-arriving usage for the month's final day: the one day a today-only
            # cycle can never revisit.
            boundary_providers = usage_ledger.providers_for_day(yesterday)
            for provider in boundary_providers:
                written, purchased = _emit_provider_day(
                    provider, yesterday, plans=plans, now_us=now_us
                )
                result["usage_rows"] += written
                if purchased:
                    result["purchase_rows"] += 1
                else:
                    result["purchase_absent"].append(f"{provider}@{previous_start}")

        # THE UNION IS LOAD-BEARING, and this is the second time it has been decided.
        #
        # I removed it once, on the argument that a provider named by
        # `providers_for_day(yesterday)` necessarily leaves Usage rows stamped
        # `not_computed` for the sweep to find. Codex refuted that at 6ab5ab3 with a
        # reproduction: `providers_for_day` and `emit_usage_rows` read
        # `usage_daily_models` in two separate connections, so a ledger rebuild landing
        # between them - `_rebuild_daily_models` deletes then re-inserts a provider-day -
        # leaves emission with ZERO rows to write. It still DELETES the previous Usage
        # rows (src/focus.py:404-410) and still resets the Purchase row
        # (src/focus.py:491-520), and the sweep then has no `not_computed` row left to
        # rediscover the provider by. The month ends with a Purchase row carrying
        # `plans_toml:cost_usd_month` and no residual: not amortised-final.
        #
        # The same hole opens with no concurrency at all whenever upstream corrects a
        # day away to nothing, which `emit_usage_rows` explicitly supports.
        #
        # Amortising a provider twice is harmless - `amortise_period` recomputes from
        # consumption and never increments - so the union costs one extra recomputation
        # in the rare case and closes the hole in every case.
        pending = focus.providers_awaiting_amortisation(previous_start)
        for provider in sorted(set(boundary_providers) | set(pending)):
            summary = focus.amortise_period(provider, previous_start, plans=plans)
            _record_amortisation(result, provider, previous_start, summary)
    except Exception as exc:  # noqa: BLE001 - same, for the boundary half
        logger.warning("focus previous-month settlement failed for %s: %s", day, exc)
        result["failures"].append(f"previous_month_failed: {exc}")

    if len(result["failures"]) > 0:
        result["state"] = STATE_FAILED
        result["reason"] = "; ".join(result["failures"])
    elif (result["usage_rows"] > 0 or result["purchase_rows"] > 0
          or len(result["amortised_periods"]) > 0):
        result["state"] = STATE_EMITTED
        result["reason"] = (
            f"{len(result['providers'])} providers, {result['usage_rows']} usage rows, "
            f"{result['purchase_rows']} purchase rows, "
            f"{len(result['amortised_periods'])} periods amortised"
        )
        if len(result["purchase_absent"]) > 0:
            # A missing Purchase row outranks the rest of the line. It is the difference
            # between a month that reconciles and one that cannot, and an operator who
            # never sees it will read the amortised Usage rows as the whole bill.
            result["reason"] += (
                f"; no plan cost for {','.join(result['purchase_absent'])}"
            )
        if len(result["unallocated_periods"]) > 0:
            result["reason"] += (
                "; nothing allocated in " + ",".join(
                    f"{label}({result['unallocated_causes'][label]})"
                    for label in result["unallocated_periods"]
                )
            )
    return result


def backfill(*, plans: dict | None = None, now_us: int | None = None) -> dict:
    """Emit and settle every provider-day the ledger holds. Never raises into the caller.

    For a fresh install, and for one that upgraded from a version where nothing called
    the emission functions. `record_cycle()` deliberately only ever looks at today, so it
    cannot fill in the past: without this, months before the upgrade stay empty forever.

    Idempotent for the same reason the cycle is - every write underneath is
    delete-then-insert or a recomputation from consumption - so running it twice, or
    running it on an install that is already up to date, changes nothing.
    """
    result: dict = {
        "state": STATE_IDLE,
        "reason": "ledger_has_no_provider_days",
        "provider_days": 0,
        "usage_rows": 0,
        "purchase_rows": 0,
        "purchase_absent": [],
        "amortised_periods": [],
        "unbalanced_periods": [],
        "unallocated_periods": [],
        "unallocated_causes": {},
        "failures": [],
    }
    try:
        pairs = usage_ledger.provider_days()
        months: set[tuple[str, str]] = set()
        for provider, day in pairs:
            result["usage_rows"] += focus.emit_usage_rows(
                provider, day, plans=plans, now_us=now_us
            )
            months.add((provider, focus._month_bounds(day)[0]))
        result["provider_days"] = len(pairs)

        for provider, month_start in sorted(months):
            if focus.emit_purchase_row(provider, month_start, plans=plans, now_us=now_us):
                result["purchase_rows"] += 1
            else:
                result["purchase_absent"].append(f"{provider}@{month_start}")
            summary = focus.amortise_period(provider, month_start, plans=plans)
            _record_amortisation(result, provider, month_start, summary)
    except Exception as exc:  # noqa: BLE001 - a backfill that dies mid-way still reports
        logger.warning("focus backfill failed: %s", exc)
        result["failures"].append(f"backfill_failed: {exc}")

    if len(result["failures"]) > 0:
        result["state"] = STATE_FAILED
        result["reason"] = "; ".join(result["failures"])
    elif result["provider_days"] > 0:
        result["state"] = STATE_EMITTED
        result["reason"] = (
            f"{result['provider_days']} provider-days, {result['usage_rows']} usage rows, "
            f"{result['purchase_rows']} purchase rows, "
            f"{len(result['amortised_periods'])} periods amortised"
        )
        if len(result["purchase_absent"]) > 0:
            result["reason"] += (
                f"; no plan cost for {','.join(result['purchase_absent'])}"
            )
        if len(result["unallocated_periods"]) > 0:
            result["reason"] += (
                "; nothing allocated in " + ",".join(
                    f"{label}({result['unallocated_causes'][label]})"
                    for label in result["unallocated_periods"]
                )
            )
    return result


def main() -> int:
    """`python3 -m src.focus_emit` - backfill all history, then report what happened."""
    outcome = backfill()
    print(f"FOCUS backfill: {outcome['state']} ({outcome['reason']})")
    if len(outcome["unbalanced_periods"]) > 0:
        # Not an error. A month is unbalanced whenever its plan cost or weekly cap is
        # unconfigured, which is a true statement about the configuration rather than a
        # failure of the backfill, and saying so beats a silent partial success.
        print(f"  did not reconcile: {', '.join(outcome['unbalanced_periods'])}")
    if len(outcome["unallocated_periods"]) > 0:
        # The line that stops "11 periods amortised" being read as "the money was
        # attributed". These months reconcile with every dollar sitting on the Purchase
        # row and none on any Usage row. Each entry carries its OWN derived reason, so
        # the operator is told which input is missing without this line guessing.
        # Grouped by cause: an operator with 11 unallocated months needs to know there
        # is ONE missing input, not eleven separate problems.
        by_cause: dict[str, list[str]] = {}
        for label in outcome["unallocated_periods"]:
            by_cause.setdefault(outcome["unallocated_causes"][label], []).append(label)
        for cause, labels in sorted(by_cause.items()):
            print(f"  nothing allocated to usage [{cause}]: {', '.join(labels)}")
    return 1 if outcome["state"] == STATE_FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
