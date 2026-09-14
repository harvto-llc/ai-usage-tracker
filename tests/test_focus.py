import csv
import io
import sqlite3

import pytest

from src import focus, usage_ledger, work_ledger


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    focus._initialized_paths.discard(str(db_path))
    return db_path


def _seed_daily_model(db_path, *, provider="claude", day="2026-08-27", model="claude-opus-4-8",
                      input_tokens=1000, output_tokens=500, cache_read=200):
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


def test_init_creates_the_focus_facts_table_idempotently(activity_db):
    focus.init()
    focus.init()  # second call must not raise or duplicate
    with sqlite3.connect(activity_db) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='focus_facts'"
        )]
    assert names == ["focus_facts"]


def test_usage_rows_carry_consumption_and_leave_effective_cost_absent(activity_db):
    _seed_daily_model(activity_db)

    written = focus.emit_usage_rows("claude", "2026-08-27")
    assert written == 1

    rows = focus.facts_for_period("2026-08-01")
    usage = [r for r in rows if r["charge_category"] == "Usage"]
    assert len(usage) == 1
    row = usage[0]
    assert row["consumed_quantity"] == 1700.0  # 1000 input + 500 output + 200 cache
    assert row["consumed_unit"] == "tokens"
    assert row["x_prompt_tokens"] == 1000
    assert row["x_output_tokens"] == 500
    assert row["x_cached_tokens"] == 200
    assert row["x_model"] == "claude-opus-4-8"
    assert row["charge_period_start"] == "2026-08-27"
    assert row["charge_period_end"] == "2026-08-28"
    # Phase 2 computes amortisation; phase 1 must not invent a number.
    assert row["effective_cost"] is None
    assert row["x_cost_basis"] == "not_computed"


def test_emitting_the_same_day_twice_replaces_rather_than_duplicates(activity_db):
    _seed_daily_model(activity_db)

    focus.emit_usage_rows("claude", "2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-27")

    usage = [r for r in focus.facts_for_period("2026-08-01")
             if r["charge_category"] == "Usage"]
    assert len(usage) == 1, "a daily job that runs twice must not double-count"


def test_a_day_with_no_usage_emits_nothing(activity_db):
    focus.init()
    assert focus.emit_usage_rows("claude", "2026-08-27") == 0
    assert focus.facts_for_period("2026-08-01") == []


def test_purchase_row_is_withheld_when_the_user_configured_no_cost(activity_db):
    # A provider the user never priced has no subscription figure. Emitting 0.00, or
    # falling back to the catalog's list price, would put an invented number into a
    # table whose whole purpose is invoice reconciliation.
    plans = {"claude": {"plan": "Max 5x"}}  # plan named, but no cost_usd_month
    assert focus.emit_purchase_row("claude", "2026-08-27", plans=plans) is False
    assert focus.emit_purchase_row("never-configured", "2026-08-27", plans=plans) is False
    assert focus.facts_for_period("2026-08-01") == []


def test_purchase_row_uses_the_users_configured_cost_not_the_catalog_list_price(activity_db):
    # UNMOCKED: exercises the real entitlements/plan_config path. A mocked resolve_plan
    # previously hid that this whole function could never emit anything.
    plans = {"claude": {"plan": "Max 5x", "cost_usd_month": 85}}

    assert focus.emit_purchase_row("claude", "2026-08-27", plans=plans) is True

    purchase = [r for r in focus.facts_for_period("2026-08-01")
                if r["charge_category"] == "Purchase"]
    assert len(purchase) == 1
    row = purchase[0]
    # 85 is what this user pays; the plan catalog's list price for Max 5x is 100.
    assert row["billed_cost"] == 85.0
    assert row["x_cost_basis"] == "plans_toml:cost_usd_month"
    # Emission leaves this absent; amortise_period() later fills it with the residual
    # the month's Usage rows did not take. Writing the full monthly charge here on top
    # of those shares would make SUM(effective_cost) double the real charge.
    assert row["effective_cost"] is None
    assert row["billing_period_start"] == "2026-08-01"
    assert row["billing_period_end"] == "2026-09-01"
    assert row["x_billing_period_basis"] == "calendar_month"
    assert row["created_at"] > 0, "created_at must be a real timestamp, not 0"


def test_purchase_row_emission_is_idempotent(activity_db):
    plans = {"claude": {"plan": "Max 5x", "cost_usd_month": 85}}
    focus.emit_purchase_row("claude", "2026-08-27", plans=plans)
    focus.emit_purchase_row("claude", "2026-08-30", plans=plans)  # same month
    purchase = [r for r in focus.facts_for_period("2026-08-01")
                if r["charge_category"] == "Purchase"]
    assert len(purchase) == 1


def test_purchase_and_usage_rows_coexist_on_the_first_of_the_month(activity_db):
    # The identity index keys on charge_period_start; a Purchase row for 2026-08-01
    # and a Usage row for the same date must not collide.
    _seed_daily_model(activity_db, day="2026-08-01")
    plans = {"claude": {"plan": "Max 5x", "cost_usd_month": 85}}

    assert focus.emit_usage_rows("claude", "2026-08-01", plans=plans) == 1
    assert focus.emit_purchase_row("claude", "2026-08-01", plans=plans) is True

    rows = focus.facts_for_period("2026-08-01")
    assert sorted(r["charge_category"] for r in rows) == ["Purchase", "Usage"]


def test_two_providers_and_two_models_on_one_day_do_not_collide(activity_db):
    _seed_daily_model(activity_db, provider="claude", model="claude-opus-4-8")
    _seed_daily_model(activity_db, provider="claude", model="claude-sonnet-5")
    _seed_daily_model(activity_db, provider="codex", model="gpt-5.6")

    assert focus.emit_usage_rows("claude", "2026-08-27") == 2
    assert focus.emit_usage_rows("codex", "2026-08-27") == 1
    assert len(focus.facts_for_period("2026-08-01")) == 3


def test_reemitting_one_day_leaves_another_days_rows_intact(activity_db):
    _seed_daily_model(activity_db, day="2026-08-26")
    _seed_daily_model(activity_db, day="2026-08-27")
    focus.emit_usage_rows("claude", "2026-08-26")
    focus.emit_usage_rows("claude", "2026-08-27")

    focus.emit_usage_rows("claude", "2026-08-27")  # re-run one day only

    days = sorted(r["charge_period_start"] for r in focus.facts_for_period("2026-08-01"))
    assert days == ["2026-08-26", "2026-08-27"]


def test_usage_rows_are_removed_when_upstream_usage_is_corrected_away(activity_db):
    # Emission makes the day MATCH upstream. If the source rows are corrected to
    # nothing, previously emitted rows must not survive claiming consumption that
    # upstream no longer believes happened.
    _seed_daily_model(activity_db)
    assert focus.emit_usage_rows("claude", "2026-08-27") == 1

    with sqlite3.connect(activity_db) as conn:
        conn.execute("DELETE FROM usage_daily_models WHERE day = '2026-08-27'")

    assert focus.emit_usage_rows("claude", "2026-08-27") == 0
    assert focus.facts_for_period("2026-08-01") == []


def test_impossible_month_is_rejected_rather_than_written(activity_db):
    plans = {"claude": {"plan": "Max 5x", "cost_usd_month": 85}}
    with pytest.raises(ValueError):
        focus.emit_purchase_row("claude", "2026-13-05", plans=plans)
    assert focus.facts_for_period("2026-13-01") == []


def test_december_month_bounds_roll_into_the_next_year(activity_db):
    assert focus._month_bounds("2026-12-14") == ("2026-12-01", "2027-01-01")
    assert focus._month_bounds("2026-01-31") == ("2026-01-01", "2026-02-01")


def test_next_day_crosses_month_and_leap_year_boundaries(activity_db):
    assert focus._next_day("2026-08-31") == "2026-09-01"
    assert focus._next_day("2026-12-31") == "2027-01-01"
    assert focus._next_day("2028-02-28") == "2028-02-29"  # 2028 is a leap year


# ── Phase 2: EffectiveCost amortisation ──────────────────

# A plan the user configured themselves: a real monthly cost and a real weekly cap.
# Both are read from plans.toml in production; injected here so no test touches the
# user's own config file.
_PLANS = {
    "claude": {
        "plan": "max-5x",
        "cost_usd_month": 100.0,
        "self_quota": {"weekly_cap_tokens": 1_000_000},
    }
}
_NO_CAP = {"claude": {"plan": "max-5x", "cost_usd_month": 100.0}}
_NO_COST = {"claude": {"plan": "max-5x", "self_quota": {"weekly_cap_tokens": 1_000_000}}}


def _seeded_month(db_path, plans, **kwargs):
    """One provider-month with its Purchase row and one day of Usage."""
    _seed_daily_model(db_path, **kwargs)
    day = kwargs.get("day", "2026-08-27")
    focus.emit_purchase_row("claude", day, plans=plans)
    focus.emit_usage_rows("claude", day, plans=plans)


def _rows_by_category(period="2026-08-01"):
    rows = focus.facts_for_period(period)
    return (
        [r for r in rows if r["charge_category"] == "Usage"],
        [r for r in rows if r["charge_category"] == "Purchase"],
    )


def test_usage_rows_take_the_upstream_share_of_the_subscription(activity_db):
    _seeded_month(activity_db, _PLANS)

    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    usage, _ = _rows_by_category()
    expected = (1700 / 1_000_000) * (100.0 / focus.WEEKS_PER_MONTH)
    assert usage[0]["effective_cost"] == pytest.approx(expected)
    assert usage[0]["x_cost_basis"] == (
        "amortised:consumed/weekly_cap*monthly_cost/weeks_per_month"
    )
    assert result["amortised_rows"] == 1
    assert result["unamortised_rows"] == 0


def test_the_month_totals_the_bill_exactly_once(activity_db):
    """The double-count invariant. Upstream would bill $100 twice; this bills it once."""
    _seed_daily_model(activity_db, day="2026-08-26", model="claude-sonnet-4-8",
                      input_tokens=20_000, output_tokens=5_000, cache_read=1_000)
    _seeded_month(activity_db, _PLANS)  # a second day, a second model
    focus.emit_usage_rows("claude", "2026-08-26", plans=_PLANS)

    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    usage, purchase = _rows_by_category()
    assert len(usage) == 2 and len(purchase) == 1
    total = sum(row["effective_cost"] for row in usage) + purchase[0]["effective_cost"]
    assert total == pytest.approx(100.0, abs=1e-9), "the subscription must be counted once"
    assert result["balanced"] is True
    # The residual is what was paid for and not used, and the bill itself is untouched.
    assert purchase[0]["effective_cost"] < 100.0
    assert purchase[0]["billed_cost"] == 100.0
    assert purchase[0]["x_cost_basis"] == (
        "residual_unused_capacity:plan_cost_minus_amortised_usage"
    )


def test_an_unknown_cap_leaves_usage_absent_and_the_bill_whole(activity_db):
    _seeded_month(activity_db, _NO_CAP)

    result = focus.amortise_period("claude", "2026-08-01", plans=_NO_CAP)

    usage, purchase = _rows_by_category()
    assert usage[0]["effective_cost"] is None, "an unmeasured cost is absent, never 0.00"
    assert usage[0]["x_cost_basis"] == "cap_unknown:no_plans_toml_self_quota_weekly_cap_tokens"
    # The bill is real even when it cannot be allocated, and it is still counted once.
    assert purchase[0]["effective_cost"] == 100.0
    assert purchase[0]["x_cost_basis"] == "unallocated_subscription:cap_unknown"
    assert result["balanced"] is True
    assert result["weekly_cap_tokens"] is None


def test_an_unknown_plan_cost_amortises_nothing_at_all(activity_db):
    _seeded_month(activity_db, _NO_COST)

    result = focus.amortise_period("claude", "2026-08-01", plans=_NO_COST)

    usage, purchase = _rows_by_category()
    assert purchase == [], "no cost is known, so there is no Purchase row to divide"
    assert usage[0]["effective_cost"] is None
    assert usage[0]["x_cost_basis"] == "plan_cost_unknown:no_plans_toml_cost_usd_month"
    assert result["balanced"] is False, "a month with no known bill does not reconcile"
    assert result["monthly_cost"] is None


def test_consuming_past_the_configured_cap_shows_a_negative_residual(activity_db):
    _seeded_month(activity_db, _PLANS, input_tokens=60_000_000, output_tokens=0, cache_read=0)

    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    usage, purchase = _rows_by_category()
    assert usage[0]["effective_cost"] > 100.0
    assert purchase[0]["effective_cost"] < 0.0, "the excess is kept, not clamped away"
    assert purchase[0]["x_cost_basis"] == (
        "residual_negative:consumption_exceeded_configured_weekly_cap"
    )
    total = usage[0]["effective_cost"] + purchase[0]["effective_cost"]
    assert total == pytest.approx(100.0, abs=1e-9)


def test_amortising_twice_recomputes_rather_than_accumulates(activity_db):
    _seeded_month(activity_db, _PLANS)

    first = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    second = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    assert first["amortised_usage_total"] == pytest.approx(second["amortised_usage_total"])
    assert first["purchase_effective_cost"] == pytest.approx(second["purchase_effective_cost"])
    usage, purchase = _rows_by_category()
    total = usage[0]["effective_cost"] + purchase[0]["effective_cost"]
    assert total == pytest.approx(100.0, abs=1e-9)


def test_another_providers_month_is_not_touched(activity_db):
    _seeded_month(activity_db, _PLANS)
    _seed_daily_model(activity_db, provider="codex", day="2026-08-27", model="gpt-5.6-sol")
    focus.emit_usage_rows("codex", "2026-08-27", plans=_PLANS)

    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    codex = [r for r in focus.facts_for_period("2026-08-01")
             if r["service_provider_name"] == "codex"]
    assert [r["effective_cost"] for r in codex] == [None]
    assert [r["x_cost_basis"] for r in codex] == ["not_computed"]


def test_a_purchase_row_that_was_never_emitted_does_not_report_a_balanced_month(activity_db):
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)

    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    assert result["purchase_rows"] == 0
    assert result["balanced"] is False, "usage amortised with no bill row does not reconcile"


# ── Phase 3: export, and the phase-1 review's unenforced constants ──

def test_an_undefined_charge_category_is_refused_rather_than_written(activity_db):
    with pytest.raises(ValueError) as exc:
        focus._checked_category("Refund")
    assert "Refund" in str(exc.value)
    for category in sorted(focus.CHARGE_CATEGORIES):
        assert focus._checked_category(category) == category


def test_a_table_made_before_reasoning_tokens_gains_the_column(activity_db, monkeypatch):
    """A database created by phase 1 must not keep returning rows without the column."""
    # The phase-1 table, built here rather than by dropping a column, so the test does
    # not depend on the SQLite version's ALTER TABLE DROP COLUMN support.
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """CREATE TABLE focus_facts(
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
                   x_messages INTEGER,
                   x_requests INTEGER,
                   x_billing_period_basis TEXT,
                   x_cost_basis TEXT,
                   created_at INTEGER NOT NULL
               )"""
        )
        names = {r[1] for r in conn.execute("PRAGMA table_info(focus_facts)")}
    assert "x_reasoning_tokens" not in names

    focus._initialized_paths.discard(str(activity_db))
    focus.init()

    with sqlite3.connect(activity_db) as conn:
        names = {r[1] for r in conn.execute("PRAGMA table_info(focus_facts)")}
    assert "x_reasoning_tokens" in names

    # And the migrated table actually takes a row carrying the value, which is the
    # claim; a present column that emission cannot write would prove nothing.
    _seed_daily_model(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("UPDATE usage_daily_models SET reasoning_tokens = 42")
    assert focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS) == 1
    assert _rows_by_category()[0][0]["x_reasoning_tokens"] == 42


def test_reasoning_tokens_are_recorded_without_inflating_consumption(activity_db):
    _seed_daily_model(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("UPDATE usage_daily_models SET reasoning_tokens = 250")

    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)

    usage, _ = _rows_by_category()
    assert usage[0]["x_reasoning_tokens"] == 250
    # Still 1000 + 500 + 200: a provider bills reasoning tokens inside its output count,
    # so adding them again would inflate the cap denominator phase 2 divides by.
    assert usage[0]["consumed_quantity"] == 1700.0


def test_export_names_the_focus_columns_and_writes_an_absent_cost_as_empty(activity_db):
    _seeded_month(activity_db, _NO_CAP)
    focus.amortise_period("claude", "2026-08-01", plans=_NO_CAP)

    text = focus.export_focus_csv("2026-08")

    header = text.splitlines()[0].split(",")
    assert header[:14] == [
        "ServiceProviderName", "ServiceName", "ChargeCategory",
        "ChargePeriodStart", "ChargePeriodEnd",
        "BillingPeriodStart", "BillingPeriodEnd",
        "PricingQuantity", "PricingUnit", "ConsumedQuantity", "ConsumedUnit",
        "BilledCost", "EffectiveCost", "BillingCurrency",
    ]
    assert "id," not in text and "created_at" not in text
    rows = list(csv.DictReader(io.StringIO(text)))
    usage = [row for row in rows if row["ChargeCategory"] == "Usage"][0]
    assert usage["EffectiveCost"] == "", "an unknown cost is an empty field, never '0'"


def test_a_period_that_is_not_a_month_is_rejected_rather_than_exported_empty(activity_db):
    focus.init()
    for period in ("2026-8", "2026-13", "august", "", "2026-08-01"):
        with pytest.raises(ValueError):
            focus.period_start(period)


def test_a_purchase_row_loses_its_residual_when_the_plan_cost_is_removed(activity_db):
    """A residual derived from a cost the user no longer declares is an invented number."""
    _seeded_month(activity_db, _PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    _, purchase = _rows_by_category()
    assert purchase[0]["effective_cost"] == pytest.approx(100.0 - (
        (1700 / 1_000_000) * (100.0 / focus.WEEKS_PER_MONTH)))

    # The user removes cost_usd_month. Emission withholds a new row rather than
    # deleting the old one, so the stale row is still there to be corrected.
    result = focus.amortise_period("claude", "2026-08-01", plans=_NO_COST)

    usage, purchase = _rows_by_category()
    assert purchase[0]["effective_cost"] is None
    assert purchase[0]["x_cost_basis"] == "plan_cost_unknown:no_plans_toml_cost_usd_month"
    assert usage[0]["effective_cost"] is None
    assert result["balanced"] is False
    # The invoice figure itself is untouched: it is what was billed, not a derivation.
    assert purchase[0]["billed_cost"] == 100.0


def test_a_month_with_no_usage_leaves_the_whole_subscription_unconsumed(activity_db):
    """Paying for a month and using none of it: the residual is the entire bill."""
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)

    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    usage, purchase = _rows_by_category()
    assert usage == []
    assert result["usage_rows"] == 0
    assert purchase[0]["effective_cost"] == 100.0
    assert purchase[0]["x_cost_basis"] == (
        "residual_unused_capacity:plan_cost_minus_amortised_usage"
    )
    assert result["balanced"] is True, "nothing consumed still totals the bill once"
