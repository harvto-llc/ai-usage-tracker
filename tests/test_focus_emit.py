"""The driveshaft, tested: does emission actually RUN, and does running it repeatedly hurt.

`src/focus.py` was complete, merged and live while nothing called it. These tests are
about the calling, not the emitting: which days a cycle touches, what a month boundary
costs, what happens when emission breaks, and whether a job that runs every 60 seconds
can run 1440 times a day without the table growing or drifting.

Re-emission stability is PROVEN here rather than taken from the docstrings. The engine's
docstrings say delete-then-insert makes it idempotent; a docstring is not a measurement,
and the scheduling layer is where a second run would actually be issued.
"""

import os
import sqlite3
from pathlib import Path

import pytest

from src import focus, focus_emit, usage_ledger


@pytest.fixture
def activity_db():
    """The isolated database `tests/conftest.py::isolate_activity_db` has already set up.

    Deliberately NOT a second copy of that isolation. A file-local fixture that set
    `USAGE_TRACKER_ACTIVITY_DB` again would mask the conftest guard from every test in
    this file - including the one that exists to prove the guard works, which passed
    against a conftest with both guards deleted until this fixture stopped duplicating
    them.
    """
    return Path(os.environ["USAGE_TRACKER_ACTIVITY_DB"])


_PLANS = {
    "claude": {
        "plan": "max-5x",
        "cost_usd_month": 100.0,
        "self_quota": {"weekly_cap_tokens": 1_000_000},
    },
    "codex": {
        "plan": "pro",
        "cost_usd_month": 20.0,
        "self_quota": {"weekly_cap_tokens": 500_000},
    },
}
_NO_COST = {"claude": {"plan": "max-5x", "self_quota": {"weekly_cap_tokens": 1_000_000}}}
# The shape of the REAL plans.toml this was validated against: a monthly cost, and no
# weekly cap to divide it by. Every Usage row then carries no cost and the whole
# subscription stays on the Purchase row - while `balanced` still comes back True.
_NO_CAP = {"claude": {"plan": "max-5x", "cost_usd_month": 200.0}}


def _seed_daily_model(db_path, *, provider="claude", day="2026-08-27",
                      model="claude-opus-4-8", input_tokens=1000, output_tokens=500,
                      cache_read=200):
    usage_ledger.init()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage_daily_models(
                   provider, day, model, messages, user_messages, sessions, requests,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens, reasoning_tokens,
                   updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (provider, day, model, 4, 2, 1, 3,
             input_tokens, output_tokens, cache_read, 0, 0, 0, 0),
        )


def _rows_without_id(period):
    """Every emitted row for a month, with the surrogate key dropped.

    `id` is AUTOINCREMENT and delete-then-insert necessarily assigns a new one, so
    comparing ids across runs would report churn that is not churn. What has to be stable
    is the row's FOCUS identity and its values, which is everything else.
    """
    return [
        {k: v for k, v in row.items() if k != "id"}
        for row in focus.facts_for_period(period)
    ]


# --------------------------------------------------------------------------------------
# The gap this package closes: a cycle emits, without anybody calling focus.py by hand.
# --------------------------------------------------------------------------------------

def test_a_collector_cycle_emits_todays_provider_days(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    _seed_daily_model(activity_db, provider="codex", day="2026-08-27",
                      model="gpt-5.4", input_tokens=400, output_tokens=100, cache_read=0)

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_EMITTED
    assert outcome["providers"] == ["claude", "codex"]
    assert outcome["usage_rows"] == 2
    assert outcome["purchase_rows"] == 2

    rows = focus.facts_for_period("2026-08-01")
    usage = [r for r in rows if r["charge_category"] == "Usage"]
    purchase = [r for r in rows if r["charge_category"] == "Purchase"]
    assert len(usage) == 2
    assert len(purchase) == 2
    # Amortisation ran in the same cycle: nothing is left carrying the emitted sentinel.
    assert [r["x_cost_basis"] for r in usage] != [focus.BASIS_NOT_COMPUTED] * 2
    assert all(r["effective_cost"] is not None for r in rows)


def test_the_cycle_amortises_the_current_month_so_it_reconciles(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    rows = focus.facts_for_period("2026-08-01")
    total = sum(r["effective_cost"] for r in rows)
    assert total == pytest.approx(100.0)


# --------------------------------------------------------------------------------------
# Running every 60 seconds: stability, proven rather than asserted from a docstring.
# --------------------------------------------------------------------------------------

def test_running_the_cycle_twice_leaves_row_counts_and_row_identities_unchanged(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27",
                      model="claude-haiku-4-5", input_tokens=90, output_tokens=10,
                      cache_read=0)
    _seed_daily_model(activity_db, provider="codex", day="2026-08-27",
                      model="gpt-5.4", input_tokens=400, output_tokens=100, cache_read=0)

    first = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)
    after_first = _rows_without_id("2026-08-01")

    second = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)
    after_second = _rows_without_id("2026-08-01")

    assert first["usage_rows"] == second["usage_rows"] == 3
    assert first["purchase_rows"] == second["purchase_rows"] == 2
    assert len(after_first) == len(after_second) == 5
    # Not just the same COUNT. The same rows: same identity, same consumption, same
    # amortised cost. A count-only assertion would pass while a re-emission silently
    # swapped one row's identity for another's.
    assert after_first == after_second


def test_twenty_cycles_in_one_day_do_not_grow_the_table(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    states = [focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)["state"]
              for _ in range(20)]

    with sqlite3.connect(activity_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM focus_facts").fetchone()[0]
    assert count == 2  # one Usage row, one Purchase row
    # And all 20 SUCCEEDED. The unique index means a re-emission that stopped deleting
    # first would raise rather than duplicate, which `record_cycle` catches - leaving the
    # row count at 2 and this test green on a cycle that had been failing for 19 minutes
    # (mutation M10). The count proves no growth; the states prove no silent breakage.
    assert states == [focus_emit.STATE_EMITTED] * 20


# --------------------------------------------------------------------------------------
# Cheapness: which days does the recurring path actually read.
# --------------------------------------------------------------------------------------

def test_the_cycle_reads_only_today_and_never_walks_history(activity_db, monkeypatch):
    for day in ("2026-03-04", "2026-05-19", "2026-08-26", "2026-08-27"):
        _seed_daily_model(activity_db, provider="claude", day=day)

    seen_days = []
    real = focus.emit_usage_rows
    monkeypatch.setattr(
        focus, "emit_usage_rows",
        lambda provider, day, **kw: (seen_days.append(day), real(provider, day, **kw))[1],
    )

    # The unbounded query is made fatal rather than merely unused. Asserting on the days
    # that came OUT cannot see a cycle that reads all of history and then emits one day
    # from it: mutation M4 swapped `providers_for_day` for `provider_days` and the
    # emitted-days assertion below still passed. The cost is in the READ, so the read is
    # what has to be forbidden.
    monkeypatch.setattr(
        usage_ledger, "provider_days",
        lambda: pytest.fail("the 60s cycle walked all of history"),
    )

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_EMITTED
    assert seen_days == ["2026-08-27"]
    # And the history it did not read stayed unemitted, rather than being emitted by a
    # month-wide query nobody noticed.
    assert focus.facts_for_period("2026-03-01") == []
    assert focus.facts_for_period("2026-05-01") == []


def test_providers_for_day_returns_only_that_days_providers(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-26")
    _seed_daily_model(activity_db, provider="codex", day="2026-08-27", model="gpt-5.4")

    assert usage_ledger.providers_for_day("2026-08-27") == ["codex"]
    assert usage_ledger.providers_for_day("2026-08-26") == ["claude"]
    assert usage_ledger.providers_for_day("2026-08-28") == []


# --------------------------------------------------------------------------------------
# The month boundary.
# --------------------------------------------------------------------------------------

def test_the_boundary_day_also_emits_the_previous_months_final_day(activity_db, monkeypatch):
    # Usage recorded for Aug 31 that only became visible to the ledger after midnight -
    # the case a today-only cycle would lose, because Aug 31 is never "today" again.
    _seed_daily_model(activity_db, provider="claude", day="2026-08-31")
    _seed_daily_model(activity_db, provider="claude", day="2026-09-01",
                      model="claude-opus-4-8", input_tokens=10, output_tokens=5,
                      cache_read=0)

    seen_days = []
    real = focus.emit_usage_rows
    monkeypatch.setattr(
        focus, "emit_usage_rows",
        lambda provider, day, **kw: (seen_days.append(day), real(provider, day, **kw))[1],
    )

    outcome = focus_emit.record_cycle(today="2026-09-01", plans=_PLANS, now_us=1)

    assert outcome["boundary_day"] is True
    assert sorted(seen_days) == ["2026-08-31", "2026-09-01"]
    august = focus.facts_for_period("2026-08-01")
    assert [r["charge_period_start"] for r in august if r["charge_category"] == "Usage"] \
        == ["2026-08-31"]


def test_the_boundary_day_leaves_the_previous_month_amortised_final(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-31")
    _seed_daily_model(activity_db, provider="claude", day="2026-09-01",
                      model="claude-opus-4-8", input_tokens=10, output_tokens=5,
                      cache_read=0)

    focus_emit.record_cycle(today="2026-09-01", plans=_PLANS, now_us=1)

    august = focus.facts_for_period("2026-08-01")
    assert len(august) == 2
    # BOTH sides settled. The boundary re-emission rewrites the previous month's Purchase
    # row back to its pre-amortisation basis, and the Usage rows back to `not_computed`;
    # it is the second of those that the sweep sees, and `amortise_period` settles both
    # sides in ONE pass, so finding the month through its Usage rows is enough to bring
    # the Purchase residual with it.
    assert all(r["effective_cost"] is not None for r in august)
    assert sum(r["effective_cost"] for r in august) == pytest.approx(100.0)
    assert [r["x_cost_basis"] for r in august if r["charge_category"] == "Purchase"] \
        == ["residual_unused_capacity:plan_cost_minus_amortised_usage"]


def test_a_non_boundary_day_does_not_re_emit_yesterday(activity_db, monkeypatch):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    seen_days = []
    real = focus.emit_usage_rows
    monkeypatch.setattr(
        focus, "emit_usage_rows",
        lambda provider, day, **kw: (seen_days.append(day), real(provider, day, **kw))[1],
    )

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    # Named for what it proves. The cycle DOES still query and settle a pending previous
    # month on an ordinary day - that is the whole point of the data-driven sweep - so a
    # name like "does not touch the previous month" would have been false.
    assert outcome["boundary_day"] is False
    assert seen_days == ["2026-08-27"]


def test_a_month_left_unamortised_is_finalised_on_a_later_day_not_only_the_first(activity_db):
    # The machine was asleep on Sep 1. A settlement driven by the calendar would have
    # missed its one chance and never noticed; this one asks the data instead.
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    assert focus.providers_awaiting_amortisation("2026-08-01") == ["claude"]

    _seed_daily_model(activity_db, provider="claude", day="2026-09-09",
                      model="claude-opus-4-8", input_tokens=10, output_tokens=5,
                      cache_read=0)
    outcome = focus_emit.record_cycle(today="2026-09-09", plans=_PLANS, now_us=1)

    assert outcome["boundary_day"] is False
    assert "claude@2026-08-01" in outcome["amortised_periods"]
    august = focus.facts_for_period("2026-08-01")
    assert all(r["effective_cost"] is not None for r in august)
    assert focus.providers_awaiting_amortisation("2026-08-01") == []


def test_awaiting_amortisation_matches_the_emitted_sentinel_and_not_null(activity_db):
    # The dangerous value is the one the schema allows and emission actually writes.
    # x_cost_basis is nullable, so a sweep written as `IS NULL` type-checks, runs, and
    # returns nothing forever - every month would report itself already settled.
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    with sqlite3.connect(activity_db) as conn:
        bases = [r[0] for r in conn.execute(
            "SELECT x_cost_basis FROM focus_facts WHERE charge_category = 'Usage'"
        )]
    assert bases == [focus.BASIS_NOT_COMPUTED]
    assert None not in bases

    assert focus.providers_awaiting_amortisation("2026-08-01") == ["claude"]
    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    assert focus.providers_awaiting_amortisation("2026-08-01") == []


def test_the_boundary_settles_a_provider_whose_day_was_corrected_away_mid_cycle(activity_db):
    """The counterexample Codex reproduced against 6ab5ab3, kept as a regression.

    `providers_for_day(yesterday)` and `emit_usage_rows` read `usage_daily_models` over
    two separate connections. A ledger rebuild landing between them - or upstream simply
    correcting the day away, which `emit_usage_rows` explicitly supports - leaves
    emission with zero rows to write. It still DELETES the previous Usage rows and still
    resets the Purchase row, so a settlement driven only by "which Usage rows still say
    not_computed" has nothing left to rediscover the provider by, and August ends
    carrying `plans_toml:cost_usd_month` with no residual: not amortised-final.

    I removed the union that prevents this, on the argument that it was unreachable. It
    is reachable. This test is the argument's refutation, kept executable.
    """
    _seed_daily_model(activity_db, provider="claude", day="2026-08-31")
    focus.emit_usage_rows("claude", "2026-08-31", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-31", plans=_PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    assert focus.providers_awaiting_amortisation("2026-08-01") == []

    # The correction lands after the provider has been discovered for the boundary day
    # but before its rows are read back.
    real_daily_models = usage_ledger.daily_models
    corrected = {"done": False}

    def correcting_daily_models(provider, start_day, end_day):
        if not corrected["done"] and start_day == "2026-08-31":
            corrected["done"] = True
            with sqlite3.connect(activity_db) as conn:
                conn.execute("DELETE FROM usage_daily_models WHERE day = '2026-08-31'")
        return real_daily_models(provider, start_day, end_day)

    usage_ledger.daily_models = correcting_daily_models
    try:
        outcome = focus_emit.record_cycle(today="2026-09-01", plans=_PLANS, now_us=1)
    finally:
        usage_ledger.daily_models = real_daily_models

    assert outcome["boundary_day"] is True
    # The sweep alone cannot see this provider: emission deleted its Usage rows and wrote
    # none back, so there is no `not_computed` row left to find.
    assert focus.providers_awaiting_amortisation("2026-08-01") == []
    # It is settled anyway, because the boundary providers are amortised explicitly.
    assert "claude@2026-08-01" in outcome["amortised_periods"]
    august = focus.facts_for_period("2026-08-01")
    purchase = [r for r in august if r["charge_category"] == "Purchase"]
    assert len(purchase) == 1
    assert purchase[0]["x_cost_basis"] != "plans_toml:cost_usd_month", \
        "the Purchase row was left at its pre-amortisation basis: month not final"
    assert purchase[0]["effective_cost"] == pytest.approx(100.0)


# --------------------------------------------------------------------------------------
# Cheapness is a property of the QUERY PLAN, not of the predicate.
# --------------------------------------------------------------------------------------

def test_the_recurring_day_query_is_an_indexed_search_not_a_full_scan(activity_db):
    """`WHERE day = ?` reads like one day and, without an index, is not one.

    The table's primary key is (provider, day, model). `day` is not its leftmost column,
    so SQLite answered this predicate with a full scan of the autoindex:

        SCAN usage_daily_models USING COVERING INDEX sqlite_autoindex_usage_daily_models_1

    which is every row of history, 1440 times a day, to publish one day. The logical
    predicate was right and the work was not, and no test that asserted which days were
    EMITTED could tell the difference. This asserts the plan.
    """
    usage_ledger.init()
    with sqlite3.connect(activity_db) as conn:
        plan = [r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT provider FROM usage_daily_models "
            "WHERE day = ? ORDER BY provider", ("2026-08-27",))]
    assert len(plan) == 1, plan
    assert plan[0].startswith("SEARCH usage_daily_models"), plan[0]
    assert "idx_usage_daily_models_day" in plan[0], plan[0]
    assert "SCAN" not in plan[0], plan[0]


# --------------------------------------------------------------------------------------
# Nothing is invented, and every skip announces itself.
# --------------------------------------------------------------------------------------

def test_a_provider_with_no_configured_cost_gets_no_purchase_row_and_is_named(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_NO_COST, now_us=1)

    assert outcome["purchase_rows"] == 0
    assert outcome["purchase_absent"] == ["claude@2026-08-01"]
    assert "no plan cost for claude@2026-08-01" in outcome["reason"]
    rows = focus.facts_for_period("2026-08-01")
    assert [r["charge_category"] for r in rows] == ["Usage"]
    # Absent, never 0.00: a zero here would read as "this subscription was free".
    assert rows[0]["effective_cost"] is None


def test_a_month_that_reconciles_without_allocating_anything_says_so(activity_db):
    """`balanced` is True here, and reporting only `balanced` would call this a success.

    This is the configuration of the install this was validated against, not a contrived
    one: `cost_usd_month` set, no `[claude.self_quota] weekly_cap_tokens`. Measured on a
    copy of the real ledger - all 479 Usage rows got effective_cost NULL with
    `cap_unknown:no_plans_toml_self_quota_weekly_cap_tokens`, all $200/month stayed on the
    Purchase row, and every one of the 11 provider-months reported balanced=True. The
    month reconciles BECAUSE nothing was allocated, and 0 + 200 == 200.
    """
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_NO_CAP, now_us=1)

    rows = focus.facts_for_period("2026-08-01")
    usage = [r for r in rows if r["charge_category"] == "Usage"]
    purchase = [r for r in rows if r["charge_category"] == "Purchase"]
    # The engine did exactly what it documents, and it does reconcile.
    assert [r["effective_cost"] for r in usage] == [None]
    assert purchase[0]["effective_cost"] == 200.0
    assert focus.amortise_period("claude", "2026-08-01", plans=_NO_CAP)["balanced"] is True
    assert outcome["unbalanced_periods"] == []

    # And the cycle says the allocation did not happen, rather than reporting the month
    # as settled and leaving an operator to infer it from the row basis.
    # The entry carries the DERIVED reason. `_NO_CAP` sets cost_usd_month and no cap,
    # so the missing input is the cap - and the report says which one, rather than
    # naming the cause this install happens to have.
    # The label keeps the same shape as every other provider-month identity in the
    # result; the cause travels beside it rather than inside it.
    assert outcome["unallocated_periods"] == ["claude@2026-08-01"]
    assert outcome["unallocated_causes"] == {
        "claude@2026-08-01": focus_emit.CAUSE_WEEKLY_CAP_UNAVAILABLE}
    assert ("nothing allocated in claude@2026-08-01(weekly_cap_unavailable)"
            in outcome["reason"])


def test_an_unpriced_month_reports_no_plan_cost_not_the_cap(activity_db):
    """The cause is DERIVED, so the two conditions do not get the same explanation.

    The first version of this reporting printed "no weekly cap configured" for every
    unallocated month, because that is the condition this install hits. Here the cap IS
    configured and the COST is not, and saying "no weekly cap" would be a statement that
    is not merely vague but false.
    """
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_NO_COST, now_us=1)

    assert outcome["unallocated_periods"] == ["claude@2026-08-01"]
    assert outcome["unallocated_causes"] == {
        "claude@2026-08-01": focus_emit.CAUSE_PLAN_COST_UNKNOWN}
    assert focus_emit.CAUSE_WEEKLY_CAP_UNAVAILABLE not in outcome["reason"]


def test_a_month_whose_consumption_is_absent_reports_consumption_unknown(activity_db):
    """The third cause: cost and cap both configured, consumption itself missing.

    `consumed_quantity` is nullable and `amortise_period` writes `consumption_unknown`
    rather than dividing by an absent numerator. A row whose consumption is 0 is NOT in
    this set - it takes a share of 0.0 and counts as amortised - so this is genuine
    absence, which is why the row is forced to NULL here rather than seeded with zeros.
    """
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("UPDATE focus_facts SET consumed_quantity = NULL "
                     "WHERE charge_category = 'Usage'")

    summary = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    assert summary["monthly_cost"] is not None
    assert summary["weekly_cap_tokens"] is not None
    assert summary["amortised_rows"] == 0
    assert focus_emit._unallocated_cause(summary) == focus_emit.CAUSE_CONSUMPTION_UNKNOWN


def test_a_free_plan_costing_zero_is_a_known_cost_not_an_absent_one(activity_db):
    """0.0 is falsy and is a MEASUREMENT. A free plan is priced, not unpriced.

    The whole cause derivation would collapse into "plan_cost_unknown" for every free
    plan if it tested `monthly_cost` for truthiness instead of `is None`, and the
    operator would be sent looking for a `cost_usd_month` they had already set.
    """
    free = {"claude": {"plan": "free", "cost_usd_month": 0.0,
                       "self_quota": {"weekly_cap_tokens": 1_000_000}}}
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=free, now_us=1)

    summary = focus.amortise_period("claude", "2026-08-01", plans=free)
    assert summary["monthly_cost"] == 0.0
    assert summary["monthly_cost"] is not None
    # A known cost of zero divides normally, so the month IS allocated.
    assert summary["amortised_rows"] == 1
    assert outcome["unallocated_periods"] == []
    assert outcome["purchase_rows"] == 1  # priced at zero, so the row is emitted


def test_a_free_plan_that_allocates_nothing_is_not_blamed_on_a_missing_price(activity_db):
    """The mutation that `test_a_free_plan_...` could not see.

    A free plan WITH a cap allocates normally, so the cause function is never reached and
    a truthiness test on `monthly_cost` survives. This forces the month to be unallocated
    for a DIFFERENT reason - consumption absent - while the cost is a known 0.0. A
    `if not summary["monthly_cost"]` would report `plan_cost_unknown` and send the
    operator to fix a price that is already set correctly.
    """
    free = {"claude": {"plan": "free", "cost_usd_month": 0.0,
                       "self_quota": {"weekly_cap_tokens": 1_000_000}}}
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27", plans=free)
    focus.emit_purchase_row("claude", "2026-08-27", plans=free)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("UPDATE focus_facts SET consumed_quantity = NULL "
                     "WHERE charge_category = 'Usage'")

    summary = focus.amortise_period("claude", "2026-08-01", plans=free)

    assert summary["monthly_cost"] == 0.0
    assert summary["amortised_rows"] == 0
    assert focus_emit._unallocated_cause(summary) == focus_emit.CAUSE_CONSUMPTION_UNKNOWN


def test_a_cap_configured_as_zero_is_unavailable_not_merely_unset(activity_db):
    """`focus._weekly_cap_tokens` returns None for a cap that is present but <= 0.

    So a summary's None cap does not mean "the user never set one", and the cause must
    not be phrased as though it did. `weekly_cap_unavailable` covers both.
    """
    zero_cap = {"claude": {"plan": "max-5x", "cost_usd_month": 200.0,
                           "self_quota": {"weekly_cap_tokens": 0}}}
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=zero_cap, now_us=1)

    summary = focus.amortise_period("claude", "2026-08-01", plans=zero_cap)
    assert summary["weekly_cap_tokens"] is None, "a cap of 0 must read as unavailable"
    assert outcome["unallocated_causes"] == {
        "claude@2026-08-01": focus_emit.CAUSE_WEEKLY_CAP_UNAVAILABLE}


def test_a_month_with_one_allocated_row_is_not_called_unallocated(activity_db):
    """"Nothing allocated" must mean nothing, not "less than everything"."""
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27",
                      model="claude-haiku-4-5", input_tokens=50, output_tokens=10,
                      cache_read=0)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("UPDATE focus_facts SET consumed_quantity = NULL "
                     "WHERE charge_category = 'Usage' AND x_model = 'claude-haiku-4-5'")

    summary = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    assert summary["amortised_rows"] == 1
    assert summary["unamortised_rows"] == 1
    result = {"amortised_periods": [], "unbalanced_periods": [],
              "unallocated_periods": [], "unallocated_causes": {}}
    focus_emit._record_amortisation(result, "claude", "2026-08-01", summary)
    assert result["unallocated_periods"] == []


def test_a_zero_consumption_month_is_allocated_and_not_flagged(activity_db):
    """0 is a measurement, not an absence: it takes a share of 0.00 and counts."""
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27",
                      input_tokens=0, output_tokens=0, cache_read=0)

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["unallocated_periods"] == []
    usage = [r for r in focus.facts_for_period("2026-08-01")
             if r["charge_category"] == "Usage"]
    assert usage[0]["consumed_quantity"] == 0.0
    assert usage[0]["effective_cost"] == 0.0  # allocated, and allocated nothing


def test_a_month_that_really_allocated_is_not_reported_as_unallocated(activity_db):
    """The other side of the same predicate: a working month must stay quiet."""
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["unallocated_periods"] == []
    assert "nothing allocated" not in outcome["reason"]
    usage = [r for r in focus.facts_for_period("2026-08-01")
             if r["charge_category"] == "Usage"]
    assert usage[0]["effective_cost"] is not None


def test_backfill_reports_the_months_where_nothing_could_be_allocated(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-07-02")
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.backfill(plans=_NO_CAP, now_us=1)

    assert outcome["purchase_rows"] == 2  # the cost IS known; the cap is not
    assert outcome["unbalanced_periods"] == []
    assert outcome["unallocated_periods"] == ["claude@2026-07-01", "claude@2026-08-01"]
    assert set(outcome["unallocated_causes"].values()) == {
        focus_emit.CAUSE_WEEKLY_CAP_UNAVAILABLE}
    assert ("nothing allocated in claude@2026-07-01(weekly_cap_unavailable),"
            "claude@2026-08-01(weekly_cap_unavailable)") in outcome["reason"]


def test_an_idle_day_announces_itself_rather_than_returning_silently(activity_db):
    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_IDLE
    # The literal, not the constant. Asserting `== focus_emit.REASON_NO_USAGE_TODAY`
    # compares the constant against itself, so emptying it to "" left this test green
    # (mutation M9) while the collector printed "FOCUS emit: idle ()" - a skip that no
    # longer announces anything.
    assert outcome["reason"] == "no_provider_days_today_and_nothing_pending"
    assert outcome["usage_rows"] == 0
    assert outcome["providers"] == []


# --------------------------------------------------------------------------------------
# Failure isolation: emission must not break collection.
# --------------------------------------------------------------------------------------

def test_an_emission_failure_becomes_a_named_state_and_never_raises(activity_db, monkeypatch):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(focus, "emit_usage_rows", boom)

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_FAILED
    assert "today_failed: database is locked" in outcome["reason"]


def test_a_failure_in_the_today_phase_still_lets_the_previous_month_settle(
    activity_db, monkeypatch
):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    _seed_daily_model(activity_db, provider="claude", day="2026-09-09",
                      model="claude-opus-4-8", input_tokens=10, output_tokens=5,
                      cache_read=0)

    monkeypatch.setattr(
        usage_ledger, "providers_for_day",
        lambda day: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")),
    )

    outcome = focus_emit.record_cycle(today="2026-09-09", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_FAILED
    assert "today_failed: no such table" in outcome["reason"]
    # The phase that could still run, ran. One broken half must not cost the other.
    assert "previous_month_failed" not in outcome["reason"]
    assert "claude@2026-08-01" in outcome["amortised_periods"]
    assert focus.providers_awaiting_amortisation("2026-08-01") == []


def test_a_previous_month_failure_does_not_lose_todays_emission(activity_db, monkeypatch):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    monkeypatch.setattr(
        focus, "providers_awaiting_amortisation",
        lambda period: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
    )

    outcome = focus_emit.record_cycle(today="2026-08-27", plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_FAILED
    assert "previous_month_failed: disk I/O error" in outcome["reason"]
    assert outcome["usage_rows"] == 1
    assert len(focus.facts_for_period("2026-08-01")) == 2


def _stub_collector(monkeypatch, collector):
    """Reduce a collector cycle to the parts this file is about, as test_fleet_push does."""
    for name, value in [
        ("scan_cc_messages_today", {"total_messages": 3}),
        ("scan_cc_tokens_today", {"total_tokens": 10}),
        ("scan_cc_projects_today", {}),
        ("scan_codex_thread_stats", {}),
        ("scan_codex_sessions", {"messages_today": 1, "input_tokens_today": 2,
                                 "output_tokens_today": 3}),
        ("load_claude_code_stats", {}),
        ("shape_claude_code_stats", {}),
        ("build_provider_snapshots", []),
    ]:
        monkeypatch.setattr(collector, name, (lambda v: lambda *a, **k: v)(value))

    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(collector.urllib.request, "urlopen", lambda *a, **k: _Response())
    monkeypatch.setattr(collector, "sync", lambda: None)
    monkeypatch.setenv("USAGE_TRACKER_ACCESS", "api")


def test_the_collector_cycle_survives_a_focus_emission_that_raises(
    activity_db, monkeypatch, capsys
):
    """FOCUS emission is additive: a broken one must not stop local collection.

    Asserting only "the POST went out" would pass with the FOCUS call DELETED from
    `collector.main` entirely - the strongest possible way for emission not to break
    collection, and not what this test claims. So it counts the invocation and reads
    back the named failed state the collector printed.
    """
    import src.collector as collector

    def explode(*args, **kwargs):
        raise RuntimeError("focus half is broken")

    # Usage must EXIST for today, or the cycle returns at the idle path without reaching
    # these stubs and the test would pass while exercising none of the emission half.
    _seed_daily_model(activity_db, provider="claude", day=focus_emit.date.today().isoformat())
    calls = []
    real_record_cycle = focus_emit.record_cycle
    monkeypatch.setattr(
        collector.focus_emit, "record_cycle",
        lambda **kw: (calls.append(kw), real_record_cycle(**kw))[1],
    )
    monkeypatch.setattr(collector.focus_emit.focus, "emit_usage_rows", explode)
    monkeypatch.setattr(collector.focus_emit.focus, "providers_awaiting_amortisation", explode)
    _stub_collector(monkeypatch, collector)

    posted = []
    monkeypatch.setattr(
        collector.urllib.request, "urlopen",
        lambda request, timeout=None: posted.append(request.full_url) or _NoopResponse(),
    )

    collector.main()

    assert len(calls) == 1, "the collector never called FOCUS emission at all"
    printed = capsys.readouterr().out
    assert "FOCUS emit: failed (today_failed: focus half is broken" in printed
    assert "previous_month_failed: focus half is broken" in printed
    assert posted == [collector.API_URL], "the local report did not go out"


class _NoopResponse:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_the_collector_reports_the_focus_state_rather_than_staying_silent(
    activity_db, monkeypatch, capsys
):
    """A skip is a fail-open unless it announces itself."""
    import src.collector as collector

    _stub_collector(monkeypatch, collector)

    collector.main()

    printed = capsys.readouterr().out
    # No usage recorded for today in this isolated database, so the honest line is the
    # idle one - and it is PRINTED, rather than the step returning quietly. Spelled out
    # rather than interpolated from the constants, for the reason mutation M9 gave.
    assert "FOCUS emit: idle (no_provider_days_today_and_nothing_pending)" in printed


# --------------------------------------------------------------------------------------
# The guard that keeps the suite out of the developer's real data.
# --------------------------------------------------------------------------------------

def test_the_suite_never_resolves_the_developers_real_activity_database():
    """Wiring emission into the collector made the whole suite a writer of this file.

    Measured before the guard existed: with `isolate_activity_db` disabled, running
    `tests/test_fleet_push.py::test_the_collector_reports_the_fleet_state_rather_than_
    staying_silent` alone wrote 2 rows into `focus_facts` at whatever path resolves - on
    a real machine, the 1.9 GB production database. That test file has no reason to know
    FOCUS emission exists.

    Asserted as a property of the RESOLVED path rather than of the fixture, because the
    fixture guards it two independent ways (an explicit `USAGE_TRACKER_ACTIVITY_DB` and a
    redirected `HOME`) and a test naming either one would pass while the other was
    removed.
    """
    import os
    import pwd
    from pathlib import Path

    resolved = focus._db_path().resolve()
    assert resolved == usage_ledger._db_path().resolve(), \
        "focus and usage_ledger must agree on which database they are using"
    # The real home is whatever HOME would be WITHOUT the fixture. Read from the password
    # database, which the fixture cannot redirect, rather than from HOME.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert not str(resolved).startswith(str(real_home / ".usage-tracker")), \
        f"the suite resolved the developer's real database: {resolved}"


def test_the_isolation_survives_the_fixture_being_deleted(tmp_path):
    """The guard must be structural, because the fixture form of it already failed.

    While measuring the per-test fixture's cost I renamed it and ran the suite, and
    `collector.main()` - reached from `tests/test_fleet_push.py`, which knows nothing
    about FOCUS - wrote 7 real rows into this machine's production database at
    2026-08-29 07:35:09Z. The row COUNT never moved, because emission is
    delete-then-insert; it replaced correctly-stamped rows and only `x_install_id` and
    `created_at` showed it.

    So the redirect is set at conftest import time, where deleting a fixture cannot
    reopen it. This runs a real pytest subprocess with the autouse fixture stripped and
    asserts the module-level floor still holds.
    """
    import subprocess
    import sys
    import textwrap

    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent("""
        import pwd, os
        from pathlib import Path
        from src import focus

        def test_resolved_path_is_not_the_real_database():
            real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
            resolved = focus._db_path().resolve()
            assert not str(resolved).startswith(str(real_home / ".usage-tracker")), resolved
    """))
    conftest = tmp_path / "conftest.py"
    # The real conftest with the per-test fixture DELETED - exactly what I did by hand.
    source = Path("tests/conftest.py").read_text()
    marker = '@pytest.fixture(autouse=True)\ndef isolate_activity_db'
    assert marker in source, "the fixture this test strips has been renamed"
    conftest.write_text(source[:source.index(marker)])

    # The subprocess must start CLEAN. Inheriting this process's environment would hand
    # it the redirect the parent conftest already set, and the test would pass without
    # the stripped conftest doing anything at all.
    import pwd

    env = {k: v for k, v in os.environ.items() if not k.startswith("USAGE_TRACKER")}
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    env["PYTHONPATH"] = str(Path.cwd())
    assert not any(k.startswith("USAGE_TRACKER") for k in env)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=Path.cwd(), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_suite_never_reads_the_developers_real_session_history():
    """The other half of the guard: isolating the database must not redirect the reads.

    An isolated but EMPTY activity database convinces `usage_ledger.sync_provider` it has
    never indexed any of the developer's sessions, so it reindexes all of them from
    `Path.home()`. Measured inside one `GET /stats`: 98.8s, 2645 real JSONL files, 8.8M
    JSON lines parsed. Isolating the writes without the reads swaps a write leak for a
    read leak and a 2x suite.
    """
    import os
    import pwd
    from pathlib import Path

    from src.scanners import claude_jsonl_files

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert Path.home().resolve() != real_home, "the suite is pointed at the real home"
    assert claude_jsonl_files() == [], \
        "the suite enumerated the developer's real Claude session files"


# --------------------------------------------------------------------------------------
# The backfill entrypoint.
# --------------------------------------------------------------------------------------

def test_backfill_covers_every_provider_day_in_the_ledger(activity_db):
    for day in ("2026-06-10", "2026-07-02", "2026-07-03", "2026-08-27"):
        _seed_daily_model(activity_db, provider="claude", day=day)
    _seed_daily_model(activity_db, provider="codex", day="2026-07-02", model="gpt-5.4")

    outcome = focus_emit.backfill(plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_EMITTED
    assert outcome["provider_days"] == 5
    assert outcome["usage_rows"] == 5
    # One Purchase row per (provider, month): claude in 06, 07, 08 and codex in 07.
    assert outcome["purchase_rows"] == 4
    assert sorted(outcome["amortised_periods"]) == [
        "claude@2026-06-01", "claude@2026-07-01", "claude@2026-08-01", "codex@2026-07-01",
    ]
    for period in ("2026-06-01", "2026-07-01", "2026-08-01"):
        rows = focus.facts_for_period(period)
        assert rows, f"{period} stayed empty"
        assert all(r["effective_cost"] is not None for r in rows)


def test_backfill_run_twice_changes_nothing(activity_db):
    for day in ("2026-06-10", "2026-07-02", "2026-08-27"):
        _seed_daily_model(activity_db, provider="claude", day=day)

    focus_emit.backfill(plans=_PLANS, now_us=1)
    first = {p: _rows_without_id(p)
             for p in ("2026-06-01", "2026-07-01", "2026-08-01")}
    focus_emit.backfill(plans=_PLANS, now_us=1)
    second = {p: _rows_without_id(p)
              for p in ("2026-06-01", "2026-07-01", "2026-08-01")}

    assert first == second


def test_backfill_names_the_provider_months_it_could_not_price(activity_db):
    _seed_daily_model(activity_db, provider="claude", day="2026-07-02")
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")

    outcome = focus_emit.backfill(plans=_NO_COST, now_us=1)

    assert outcome["purchase_rows"] == 0
    assert outcome["purchase_absent"] == ["claude@2026-07-01", "claude@2026-08-01"]
    assert "no plan cost for claude@2026-07-01,claude@2026-08-01" in outcome["reason"]


def test_backfill_on_an_empty_ledger_says_so(activity_db):
    outcome = focus_emit.backfill(plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_IDLE
    assert outcome["reason"] == "ledger_has_no_provider_days"
    assert outcome["provider_days"] == 0


def test_backfill_failure_becomes_a_named_state_and_never_raises(activity_db, monkeypatch):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    monkeypatch.setattr(
        usage_ledger, "provider_days",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")),
    )

    outcome = focus_emit.backfill(plans=_PLANS, now_us=1)

    assert outcome["state"] == focus_emit.STATE_FAILED
    assert "backfill_failed: no such table" in outcome["reason"]


def test_the_backfill_entrypoint_reports_and_exits_zero(activity_db, capsys, monkeypatch):
    _seed_daily_model(activity_db, provider="claude", day="2026-08-27")
    monkeypatch.setattr("src.plan_config.load_plans", lambda path=None: _PLANS)

    assert focus_emit.main() == 0

    out = capsys.readouterr().out
    assert "FOCUS backfill: emitted" in out
    assert "1 provider-days" in out


def test_the_backfill_entrypoint_exits_nonzero_when_it_failed(activity_db, capsys,
                                                              monkeypatch):
    monkeypatch.setattr(
        usage_ledger, "provider_days",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")),
    )

    assert focus_emit.main() == 1
    assert "FOCUS backfill: failed" in capsys.readouterr().out
