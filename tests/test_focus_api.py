import csv
import io
import os
import sqlite3

os.environ.setdefault("USAGE_TRACKER_SECRET", "test-secret")

import pytest
from fastapi.testclient import TestClient

from src import focus, plan_config, usage_ledger, work_ledger
from src.api import API_SECRET, app


AUTH = {"Authorization": f"Bearer {API_SECRET}"}

# The user's own configured plan: a real monthly cost and a real weekly cap, injected
# so no test reads the machine's plans.toml.
PLANS = {
    "claude": {
        "plan": "max-5x",
        "cost_usd_month": 100.0,
        "self_quota": {"weekly_cap_tokens": 1_000_000},
    }
}
PLANS_WITHOUT_CAP = {"claude": {"plan": "max-5x", "cost_usd_month": 100.0}}


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    focus._initialized_paths.discard(str(db_path))
    return db_path


@pytest.fixture
def client():
    return TestClient(app)


def _seed(db_path, plans, *, day="2026-08-27", reasoning=77):
    usage_ledger.init()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO usage_daily_models(
                   provider, day, model, messages, user_messages, sessions, requests,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens, reasoning_tokens,
                   updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("claude", day, "claude-opus-4-8", 4, 2, 1, 3, 1000, 500, 200, 0, 0, reasoning, 0),
        )
    focus.emit_purchase_row("claude", day, plans=plans)
    focus.emit_usage_rows("claude", day, plans=plans)


def test_the_focus_endpoints_are_authenticated(client):
    assert client.get("/focus/facts", params={"period": "2026-08"}).status_code == 401
    assert client.get("/focus/export", params={"period": "2026-08"}).status_code == 401
    assert client.get("/focus/summary", params={"period": "2026-08"}).status_code == 401
    assert client.get("/focus/reconcile", params={"period": "2026-08"}).status_code == 401


def test_facts_serialise_an_unmeasured_cost_as_null_never_zero(client, activity_db):
    _seed(activity_db, PLANS_WITHOUT_CAP)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS_WITHOUT_CAP)

    body = client.get("/focus/facts", params={"period": "2026-08"}, headers=AUTH).json()

    assert body["focus_version"] == "1.3"
    assert body["billing_period_start"] == "2026-08-01"
    usage = [row for row in body["facts"] if row["charge_category"] == "Usage"]
    assert len(usage) == 1
    assert usage[0]["effective_cost"] is None, "an unknown cost must not become 0"
    assert usage[0]["billed_cost"] is None
    assert usage[0]["x_cost_basis"] == "cap_unknown:no_plans_toml_self_quota_weekly_cap_tokens"
    assert usage[0]["x_reasoning_tokens"] == 77


def test_facts_carry_the_amortised_costs_once_amortised(client, activity_db):
    _seed(activity_db, PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS)

    body = client.get("/focus/facts", params={"period": "2026-08"}, headers=AUTH).json()

    costs = {row["charge_category"]: row["effective_cost"] for row in body["facts"]}
    assert costs["Usage"] == pytest.approx((1700 / 1_000_000) * (100.0 / focus.WEEKS_PER_MONTH))
    assert costs["Usage"] + costs["Purchase"] == pytest.approx(100.0, abs=1e-9)


def test_export_emits_focus_column_names_and_leaves_an_absent_cost_empty(client, activity_db):
    _seed(activity_db, PLANS_WITHOUT_CAP)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS_WITHOUT_CAP)

    response = client.get(
        "/focus/export", params={"period": "2026-08", "format": "csv"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="focus-2026-08.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0].keys() >= {
        "ServiceProviderName", "ChargeCategory", "BilledCost", "EffectiveCost",
        "ConsumedQuantity", "BillingPeriodStart", "x_ReasoningTokens",
    }
    usage = [row for row in rows if row["ChargeCategory"] == "Usage"][0]
    assert usage["EffectiveCost"] == "", "an absent cost is an empty field, never '0'"
    assert usage["BilledCost"] == ""
    assert usage["ConsumedQuantity"] == "1700.0"
    assert usage["x_ReasoningTokens"] == "77"
    purchase = [row for row in rows if row["ChargeCategory"] == "Purchase"][0]
    assert purchase["BilledCost"] == "100.0"


def test_summary_reconciles_the_month_against_the_plan_cost(client, activity_db, monkeypatch):
    monkeypatch.setattr(plan_config, "load_plans", lambda *a, **k: PLANS)
    _seed(activity_db, PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS)

    body = client.get("/focus/summary", params={"period": "2026-08"}, headers=AUTH).json()

    assert body["focus_version"] == "1.3"
    provider = body["providers"][0]
    assert provider["service_provider_name"] == "claude"
    assert provider["effective_cost_total"] == pytest.approx(100.0, abs=1e-9)
    assert provider["monthly_plan_cost"] == 100.0
    assert provider["reconciles"] is True
    assert provider["rows_without_effective_cost"] == 0
    # Units are never added together: a seat-month is not a token.
    assert provider["consumed_by_unit"] == {"seat-month": 1.0, "tokens": 1700.0}


def test_summary_counts_unpriced_rows_rather_than_treating_them_as_free(
    client, activity_db, monkeypatch
):
    monkeypatch.setattr(plan_config, "load_plans", lambda *a, **k: PLANS_WITHOUT_CAP)
    _seed(activity_db, PLANS_WITHOUT_CAP)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS_WITHOUT_CAP)

    provider = client.get(
        "/focus/summary", params={"period": "2026-08"}, headers=AUTH
    ).json()["providers"][0]

    assert provider["rows_without_effective_cost"] == 1
    # The bill is still counted once: all of it sits on the Purchase row.
    assert provider["effective_cost_total"] == 100.0
    assert provider["reconciles"] is True


def test_a_period_with_no_priced_rows_totals_null_rather_than_zero(
    client, activity_db, monkeypatch
):
    no_cost = {"claude": {"plan": "max-5x", "self_quota": {"weekly_cap_tokens": 1_000_000}}}
    monkeypatch.setattr(plan_config, "load_plans", lambda *a, **k: no_cost)
    _seed(activity_db, no_cost)
    focus.amortise_period("claude", "2026-08-01", plans=no_cost)

    provider = client.get(
        "/focus/summary", params={"period": "2026-08"}, headers=AUTH
    ).json()["providers"][0]

    assert provider["effective_cost_total"] is None, "nothing priced is not $0 spent"
    assert provider["billed_cost_total"] is None
    assert provider["monthly_plan_cost"] is None
    assert provider["reconciles"] is False


def test_an_unusable_period_is_rejected_rather_than_answered_empty(client):
    for period in ("2026-8", "august", "2026-13", ""):
        for path in ("/focus/facts", "/focus/summary", "/focus/export"):
            response = client.get(path, params={"period": period}, headers=AUTH)
            assert response.status_code in (400, 422), f"{path} accepted {period!r}"


def test_an_unsupported_export_format_is_refused(client):
    response = client.get(
        "/focus/export", params={"period": "2026-08", "format": "parquet"}, headers=AUTH
    )
    assert response.status_code == 400
    assert "parquet" in response.json()["detail"]


def test_reconcile_reports_the_difference_between_billed_and_derived(client, activity_db):
    from src import invoice_ingest

    _seed(activity_db, PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=PLANS)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,112.50\n"
    )

    body = client.get(
        "/focus/reconcile", params={"period": "2026-08"}, headers=AUTH
    ).json()

    comparison = body["providers"][0]["comparisons"][0]
    assert comparison["comparable"] is True
    assert comparison["difference"] == pytest.approx(12.50)


def test_reconcile_serialises_an_unavailable_difference_as_null_never_zero(client, activity_db):
    """0 would read as "the invoice matched"; the finding is "we could not tell"."""
    from src import invoice_ingest

    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,112.50\n"
    )

    body = client.get(
        "/focus/reconcile", params={"period": "2026-08"}, headers=AUTH
    ).json()

    comparison = body["providers"][0]["comparisons"][0]
    assert comparison["difference"] is None
    assert comparison["difference_absent_reason"] == (
        "no_telemetry_rows_for_this_provider_period"
    )


def test_reconcile_rejects_a_period_that_is_not_a_month(client, activity_db):
    response = client.get("/focus/reconcile", params={"period": "2026-8"}, headers=AUTH)
    assert response.status_code == 400
    assert "YYYY-MM" in response.json()["detail"]


def test_reconcile_announces_a_provider_whose_surface_closed(client, activity_db):
    """Coverage that shrinks in silence is the failure this endpoint has to prevent."""
    from src import invoice_ingest

    _seed(activity_db, PLANS, day="2026-07-15")
    focus.amortise_period("claude", "2026-07-01", plans=PLANS)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-07-01,2026-08-01,USD,100.00\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00\n"
    )

    body = client.get(
        "/focus/reconcile", params={"period": "2026-08"}, headers=AUTH
    ).json()

    assert body["degraded_providers"] == ["claude"]
    coverage = body["providers"][0]["coverage"]
    assert coverage["source_coverage_pct"] == 50.0
    assert coverage["sources_absent"] == ["telemetry"]
    assert "source_absent:telemetry" in coverage["coverage_reasons"]
    # The row-based number stays reassuring, which is why it is not the one that reports
    # a closed surface.
    assert coverage["pricing_coverage_pct"] == 100.0
