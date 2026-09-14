"""Invoice-side ingestion: the second supply route into `focus_facts`.

Roadmap item 4. These tests exist because item 2's table had no column saying where a row
came from, so an invoice row and a telemetry row for the same provider-month were the same
row as far as the emission path was concerned.
"""

import csv
import io
import sqlite3
from pathlib import Path

import pytest

from src import focus, invoice_ingest, usage_ledger, work_ledger

FIXTURES = Path(__file__).parent / "fixtures" / "invoices"

_PLANS = {"claude": {"cost_usd_month": 100.0, "self_quota": {"weekly_cap_tokens": 1_000_000}}}


@pytest.fixture(autouse=True)
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "activity.db"
    monkeypatch.setenv("USAGE_TRACKER_ACTIVITY_DB", str(db_path))
    usage_ledger._initialized_paths.discard(str(db_path))
    work_ledger._initialized_paths.discard(str(db_path))
    focus._initialized_paths.discard(str(db_path))
    return db_path


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


def _fixture_text(name="claude-2026-08.csv"):
    return (FIXTURES / name).read_text()


def _rows(period="2026-08-01"):
    return focus.facts_for_period(period)


# --- the import contract -----------------------------------------------------------


def test_every_import_column_is_a_focus_column_the_export_already_emits():
    """Derived from FOCUS_CSV_COLUMNS itself, not from a copy of it.

    A hand-written list would go stale the first time the export gained or renamed a
    column, and go stale silently, which is the failure mode this check exists to avoid.
    """
    exported = {column for column, _ in focus.FOCUS_CSV_COLUMNS}
    contract = set(invoice_ingest.REQUIRED_COLUMNS) | set(invoice_ingest.OPTIONAL_COLUMNS)
    assert contract <= exported, sorted(contract - exported)


def test_the_shipped_fixture_ingests_under_its_own_documented_contract(activity_db):
    report = invoice_ingest.ingest_csv(_fixture_text())
    assert report["rejections"] == []
    assert report["written"] is True
    assert report["rows_ingested"] == 3
    assert report["source"] == focus.SOURCE_INVOICE


def test_the_fixture_ships_with_a_source_note_that_dates_its_provenance():
    """Whitespace-normalised before matching: the claims wrap across lines in the note."""
    note = " ".join((FIXTURES / "SOURCE.md").read_text().split())
    assert "Access date: **2026-08-27** (America/Los_Angeles" in note
    # It must say what it is NOT, or the next reader cites it as a vendor's real format.
    assert "NOT a copy of any vendor's file" in note
    # And it must carry the checks behind that claim, not just the claim.
    assert "HTTP Error 403: Forbidden" in note
    assert "git ls-files" in note


# --- D1: the migration, proven on a POPULATED database -----------------------------


_PRE_MIGRATION_TABLE = """
    CREATE TABLE focus_facts(
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
        created_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX idx_focus_facts_identity
    ON focus_facts(
        service_provider_name, charge_category,
        charge_period_start, COALESCE(x_model, '')
    );
"""


def _populate_pre_migration(db_path):
    """A database as item 2 left it: no x_source column, narrow identity, real rows."""
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_PRE_MIGRATION_TABLE)
        conn.executemany(
            """INSERT INTO focus_facts(
                   service_provider_name, service_name, charge_category,
                   charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end,
                   consumed_quantity, consumed_unit, billed_cost, effective_cost,
                   x_model, x_billing_period_basis, x_cost_basis, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("claude", "Claude Max", "Purchase", "2026-08-01", "2026-09-01",
                 "2026-08-01", "2026-09-01", 1.0, "seat-month", 100.0, 95.0,
                 None, "calendar_month", "plans_toml:cost_usd_month", 1),
                ("claude", "Claude Max", "Usage", "2026-08-27", "2026-08-28",
                 "2026-08-01", "2026-09-01", 1700.0, "tokens", None, 5.0,
                 "claude-opus-4-8", "calendar_month", "amortised:x", 2),
            ],
        )
    focus._initialized_paths.discard(str(db_path))


def test_a_populated_database_keeps_every_row_and_gains_a_defined_source(activity_db):
    """The migration must not lose a user's rows, and must not leave them NULL either."""
    _populate_pre_migration(activity_db)
    with sqlite3.connect(activity_db) as conn:
        before = conn.execute(
            "SELECT id, billed_cost, effective_cost FROM focus_facts ORDER BY id"
        ).fetchall()
    assert len(before) == 2

    focus.init()

    with sqlite3.connect(activity_db) as conn:
        after = conn.execute(
            "SELECT id, billed_cost, effective_cost FROM focus_facts ORDER BY id"
        ).fetchall()
        sources = [r[0] for r in conn.execute(
            "SELECT x_source FROM focus_facts ORDER BY id")]
    assert after == before, "no row and no stored figure may change in the migration"
    # A DEFINED value, not NULL-by-accident: every row that can predate the column was
    # written by telemetry emission, so 'telemetry' is a fact about these rows.
    assert sources == [focus.SOURCE_TELEMETRY, focus.SOURCE_TELEMETRY]
    assert None not in sources


def test_the_migration_replaces_the_narrow_identity_under_a_new_index_name(activity_db):
    """A new NAME, because CREATE UNIQUE INDEX IF NOT EXISTS on the old one is a no-op.

    Reusing the name would leave every existing user database on the OLD identity while
    fresh databases got the new one - a fail-open no fresh-database test could ever see.
    """
    _populate_pre_migration(activity_db)
    focus.init()
    with sqlite3.connect(activity_db) as conn:
        indexes = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='focus_facts'")}
    assert "idx_focus_facts_identity" not in indexes, "the narrow identity must be gone"
    assert "idx_focus_facts_identity_v2" in indexes
    assert "x_source" in indexes["idx_focus_facts_identity_v2"]


def test_the_migration_reverses_on_a_populated_database_with_a_colliding_row(activity_db):
    """The downgrade recipe, run on the case that makes it hard, not on an easy one.

    Hard case: a pre-migration telemetry Purchase row for claude/2026-08-01 with no
    model, and an invoice Purchase row that shares EXACTLY that old four-part identity.
    Under the old narrow index the two cannot coexist, so the downgrade has to remove one
    of them before the original index can be recreated at all.

    What the recipe guarantees and what it does not:
    - every row that existed BEFORE the migration survives, untouched;
    - the original narrow identity is enforced again afterwards;
    - rows added AFTER the migration by the new feature are REMOVED. That is deliberate
      feature-data removal, not a bug: they are rows the downgraded schema has no way to
      represent, and keeping them would leave a database the old index cannot describe.
    """
    _populate_pre_migration(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.row_factory = sqlite3.Row
        before = [dict(r) for r in conn.execute("SELECT * FROM focus_facts ORDER BY id")]

    focus.init()
    invoice_ingest.ingest_csv(_fixture_text())

    colliding = [r for r in _rows()
                 if r["charge_category"] == "Purchase" and r["x_model"] is None]
    assert len(colliding) == 2, "the two rows share the pre-migration identity exactly"
    assert sorted(r["x_source"] for r in colliding) == [
        focus.SOURCE_INVOICE, focus.SOURCE_TELEMETRY
    ]

    # The downgrade, in ONE transaction: a half-applied reverse would leave the table
    # with neither identity enforced.
    with sqlite3.connect(activity_db) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            BEGIN;
            DELETE FROM focus_facts WHERE COALESCE(x_source,'telemetry') <> 'telemetry';
            DROP INDEX IF EXISTS idx_focus_facts_identity_v2;
            CREATE UNIQUE INDEX idx_focus_facts_identity
            ON focus_facts(
                service_provider_name, charge_category,
                charge_period_start, COALESCE(x_model, '')
            );
            COMMIT;
            """
        )
        after = [dict(r) for r in conn.execute("SELECT * FROM focus_facts ORDER BY id")]
        indexes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='focus_facts'")}

    # The columns the migrations ADDED are derived from the two snapshots rather than
    # listed, so this comparison cannot go stale the next time a column is added; the
    # set is then asserted exactly, so a column silently vanishing still fails here.
    added = set(after[0]) - set(before[0])
    assert added == {"x_source", "x_install_id"}
    assert [{k: v for k, v in row.items() if k not in added} for row in after] == before
    # A pre-migration row's install was never observed, so it says so rather than being
    # claimed by whichever install happened to run the upgrade.
    assert {row["x_install_id"] for row in after} == {"unknown"}
    assert "idx_focus_facts_identity" in indexes
    assert "idx_focus_facts_identity_v2" not in indexes

    # And the restored index is ENFORCING, not merely present.
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(activity_db) as conn:
            conn.execute(
                """INSERT INTO focus_facts(
                       service_provider_name, service_name, charge_category,
                       charge_period_start, charge_period_end,
                       billing_period_start, billing_period_end, created_at)
                   VALUES('claude','Claude Max','Purchase','2026-08-01','2026-09-01',
                          '2026-08-01','2026-09-01', 3)"""
            )


def test_a_v2_index_with_the_wrong_definition_is_rebuilt_rather_than_trusted(activity_db):
    """CREATE UNIQUE INDEX IF NOT EXISTS matches on the NAME, so the name is not enough."""
    _populate_pre_migration(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.execute("ALTER TABLE focus_facts ADD COLUMN x_source TEXT")
        conn.execute("DROP INDEX idx_focus_facts_identity")
        # A v2 that carries the right NAME and the WRONG identity - what an interrupted
        # or hand-edited upgrade leaves behind.
        conn.execute(
            "CREATE UNIQUE INDEX idx_focus_facts_identity_v2 "
            "ON focus_facts(service_provider_name, charge_category)"
        )
    focus._initialized_paths.discard(str(activity_db))

    focus.init()

    with sqlite3.connect(activity_db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_focus_facts_identity_v2'"
        ).fetchone()[0]
    assert "x_source" in sql and "charge_period_start" in sql

    # Repeated init() must still be a no-op once the definition agrees.
    focus._initialized_paths.discard(str(activity_db))
    focus.init()
    with sqlite3.connect(activity_db) as conn:
        again = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_focus_facts_identity_v2'"
        ).fetchone()[0]
    assert again == sql


def test_rows_of_two_sources_read_back_in_a_defined_order(activity_db):
    """Two sources share the old four-part identity, so it can no longer order them."""
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    invoice_ingest.ingest_csv(_fixture_text())

    first = [(r["charge_category"], r["x_model"], r["x_source"]) for r in _rows()]
    second = [(r["charge_category"], r["x_model"], r["x_source"]) for r in _rows()]
    assert first == second
    assert first == sorted(first, key=lambda item: (item[0], item[1] or "", item[2]))


# --- the collision the item exists to fix ------------------------------------------


def test_an_invoice_row_and_a_telemetry_row_for_one_provider_month_coexist(activity_db):
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    invoice_ingest.ingest_csv(_fixture_text())

    purchases = [r for r in _rows() if r["charge_category"] == "Purchase"]
    assert len(purchases) == 2
    assert sorted(r["x_source"] for r in purchases) == [
        focus.SOURCE_INVOICE, focus.SOURCE_TELEMETRY
    ]


def test_telemetry_emission_no_longer_destroys_the_invoice_row(activity_db):
    """The DELETE, not the index, was the destroying agent. This is the regression."""
    invoice_ingest.ingest_csv(_fixture_text())
    _seed_daily_model(activity_db)

    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)

    invoiced = [r for r in _rows() if r["x_source"] == focus.SOURCE_INVOICE]
    assert len(invoiced) == 3, "re-emitting telemetry must not remove invoice rows"


def test_an_invoice_row_survives_amortisation_of_the_same_month(activity_db):
    """An invoice figure is what a vendor billed; a residual is a derivation."""
    invoice_ingest.ingest_csv(_fixture_text())
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)

    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)

    invoiced = {r["charge_category"]: r for r in _rows()
                if r["x_source"] == focus.SOURCE_INVOICE}
    assert invoiced["Purchase"]["billed_cost"] == 100.0
    assert invoiced["Purchase"]["effective_cost"] == 100.0
    assert invoiced["Purchase"]["x_cost_basis"] == (
        "invoice:billed_cost_supplied+effective_cost_supplied"
    )
    assert result["usage_rows"] == 1, "only the telemetry Usage row is amortised"
    assert result["balanced"] is True, "the telemetry month still reconciles"


def test_the_summary_does_not_double_count_the_month_an_invoice_describes(activity_db):
    """One bill counted twice - once derived, once billed - is the trap here."""
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    before = focus.summary_for_period("2026-08", plans=_PLANS)["providers"][0]

    invoice_ingest.ingest_csv(_fixture_text())
    after = focus.summary_for_period("2026-08", plans=_PLANS)["providers"][0]

    assert after["effective_cost_total"] == before["effective_cost_total"] == 100.0
    assert after["reconciles"] is True
    assert after["totals_source"] == focus.SOURCE_TELEMETRY
    # The quantity is the same trap one level down: one seat-month billed and one
    # seat-month derived would read as two seats held.
    assert after["consumed_by_unit"] == before["consumed_by_unit"]
    assert after["consumed_by_unit"]["seat-month"] == 1.0
    assert after["telemetry_rows"] == before["telemetry_rows"] == 2
    assert after["rows"] == before["rows"] + 3
    by_source = {block["x_source"]: block for block in after["by_source"]}
    assert by_source[focus.SOURCE_INVOICE]["billed_cost_total"] == 100.0
    assert by_source[focus.SOURCE_INVOICE]["rows"] == 3
    assert by_source[focus.SOURCE_INVOICE]["consumed_by_unit"] == {"seat-month": 1.0}
    assert by_source[focus.SOURCE_TELEMETRY]["consumed_by_unit"] == (
        before["consumed_by_unit"]
    )


def test_the_export_carries_the_source_of_every_row(activity_db):
    """Provenance travels out of the process too, or a spreadsheet blends the two."""
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    invoice_ingest.ingest_csv(_fixture_text())

    text = focus.export_focus_csv("2026-08")

    assert text.splitlines()[0].split(",")[-1] == "x_Source"
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {row["x_Source"] for row in rows} == {"telemetry", "invoice"}
    assert "" not in {row["x_Source"] for row in rows}


# --- zero versus absent, and failing closed ----------------------------------------


def test_a_stated_zero_is_a_measurement_and_an_empty_field_is_an_absence(activity_db):
    invoice_ingest.ingest_csv(_fixture_text())

    rows = {r["charge_category"]: r for r in _rows()
            if r["x_source"] == focus.SOURCE_INVOICE}
    # The vendor stated 0.00 tax. That IS a measurement and must survive as one.
    assert rows["Tax"]["billed_cost"] == 0.0
    # The vendor showed a Usage line with no amount. That is ABSENT, never 0.
    assert rows["Usage"]["billed_cost"] is None
    assert rows["Usage"]["effective_cost"] is None
    # The basis states BOTH facts about this row, and states them truthfully. An earlier
    # draft said `billed_cost_as_supplied` on a row whose billed cost was absent.
    assert rows["Usage"]["x_cost_basis"] == (
        "invoice:billed_cost_absent+effective_cost_absent"
    )
    assert rows["Tax"]["x_cost_basis"] == (
        "invoice:billed_cost_supplied+effective_cost_supplied"
    )


def test_a_file_with_one_bad_line_writes_nothing_and_names_every_rejection(activity_db):
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00\n"
        "claude,Subscription,2026-08-01,2026-09-01,USD,5.00\n"
        "claude,Usage,2026-08-01,2026-09-01,USD,not-a-number\n"
    )
    report = invoice_ingest.ingest_csv(text)

    assert report["written"] is False
    assert report["rows_ingested"] == 0
    assert _rows() == [], "a partial month is worse than no month"
    assert [r["reason"] for r in report["rejections"]] == [
        "charge_category_not_a_focus_category", "billed_cost_not_a_number"
    ]
    assert [r["line"] for r in report["rejections"]] == [3, 4]


def test_a_column_the_contract_does_not_define_is_refused(activity_db):
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,mystery_total\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00,7\n"
    )
    with pytest.raises(invoice_ingest.IngestError, match="mystery_total"):
        invoice_ingest.ingest_csv(text)
    assert _rows() == []


def test_a_missing_required_column_is_refused_rather_than_defaulted(activity_db):
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,100.00\n"
    )
    with pytest.raises(invoice_ingest.IngestError, match="billing_currency"):
        invoice_ingest.ingest_csv(text)


def test_a_non_usd_line_is_refused_rather_than_added_into_a_usd_total(activity_db):
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,EUR,90.00\n"
    )
    report = invoice_ingest.ingest_csv(text)
    assert report["written"] is False
    assert report["rejections"][0]["reason"] == "billing_currency_unsupported"
    assert report["rejections"][0]["detail"] == "EUR"


def test_a_date_that_only_looks_like_a_date_is_rejected(activity_db):
    """A shape check passes 2026-02-31, which is not a day.

    Ingested, it would sit in the table as a charge period no real-month query could
    match, so its cost would vanish from every report while the row looked healthy.
    """
    header = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
    )
    cases = [
        ("claude,Purchase,2026-13-01,2026-14-01,USD,100.00\n",
         "charge_period_start_not_a_calendar_date"),
        ("claude,Purchase,2026-02-31,2026-03-01,USD,100.00\n",
         "charge_period_start_not_a_calendar_date"),
        ("claude,Purchase,2026-08-01,2026-14-01,USD,100.00\n",
         "charge_period_end_not_a_calendar_date"),
        ("claude,Purchase,2026-08-05,2026-08-01,USD,100.00\n",
         "charge_period_end_not_after_start"),
        ("claude,Purchase,2026-08-01,2026-08-01,USD,100.00\n",
         "charge_period_end_not_after_start"),
    ]
    for row, reason in cases:
        report = invoice_ingest.ingest_csv(header + row)
        assert report["written"] is False, row
        assert report["rejections"][0]["reason"] == reason, row
    assert _rows() == []


def test_an_undeclared_source_is_refused_at_the_point_of_writing(activity_db):
    with pytest.raises(ValueError, match="is not a known source"):
        invoice_ingest.ingest_csv(_fixture_text(), source="guesswork")


# --- idempotence and the two non-telemetry sources ---------------------------------


def test_ingesting_the_same_invoice_twice_replaces_rather_than_duplicates(activity_db):
    invoice_ingest.ingest_csv(_fixture_text())
    invoice_ingest.ingest_csv(_fixture_text())

    invoiced = [r for r in _rows() if r["x_source"] == focus.SOURCE_INVOICE]
    assert len(invoiced) == 3


def test_a_corrected_invoice_supersedes_the_month_it_names(activity_db):
    invoice_ingest.ingest_csv(_fixture_text())
    corrected = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,120.00\n"
    )
    invoice_ingest.ingest_csv(corrected)

    invoiced = [r for r in _rows() if r["x_source"] == focus.SOURCE_INVOICE]
    assert len(invoiced) == 1, "the corrected file replaces the month it names"
    assert invoiced[0]["billed_cost"] == 120.0


def test_a_customer_export_and_an_invoice_are_different_sources_that_coexist(activity_db):
    invoice_ingest.ingest_csv(_fixture_text())
    invoice_ingest.ingest_csv(_fixture_text(), source=focus.SOURCE_CUSTOMER_EXPORT)

    by_source: dict[str, int] = {}
    for row in _rows():
        by_source[row["x_source"]] = by_source.get(row["x_source"], 0) + 1
    assert by_source == {focus.SOURCE_INVOICE: 3, focus.SOURCE_CUSTOMER_EXPORT: 3}


def test_a_month_the_file_does_not_name_is_left_alone(activity_db):
    invoice_ingest.ingest_csv(_fixture_text())
    july = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-07-01,2026-08-01,USD,100.00\n"
    )
    invoice_ingest.ingest_csv(july)

    assert len([r for r in _rows("2026-08-01")]) == 3
    assert len([r for r in _rows("2026-07-01")]) == 1


def test_an_invoice_row_never_carries_invented_token_counts(activity_db):
    """An invoice states money. A 0 here would be counted as observed consumption."""
    invoice_ingest.ingest_csv(_fixture_text())
    for row in _rows():
        assert row["x_prompt_tokens"] is None
        assert row["x_output_tokens"] is None
        assert row["x_cached_tokens"] is None
        assert row["x_messages"] is None
        assert row["x_requests"] is None


def test_a_derived_billing_period_says_it_was_derived(activity_db):
    """The label tracks what happened. The fixture supplies no billing-period columns."""
    invoice_ingest.ingest_csv(_fixture_text())
    for row in _rows():
        assert row["x_billing_period_basis"] == (
            "derived_calendar_month_from_charge_period_start"
        )


def test_a_supplied_billing_period_is_used_and_labelled_as_supplied(activity_db):
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,billing_period_start,billing_period_end\n"
        "claude,Usage,2026-08-14,2026-08-15,USD,3.00,2026-08-01,2026-09-01\n"
    )
    row = _rows()[0]
    assert row["billing_period_start"] == "2026-08-01"
    assert row["billing_period_end"] == "2026-09-01"
    assert row["x_billing_period_basis"] == "source_supplied_period"


def test_a_billing_period_no_period_query_could_find_is_refused(activity_db):
    """Every period query in this repo looks a row up by a YYYY-MM-01 key.

    A row filed under a billing anniversary would be unreachable by all of them, so its
    cost would disappear from every report while the row sat in the table looking fine.
    """
    header = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,billing_period_start,billing_period_end\n"
    )
    cases = [
        ("claude,Purchase,2026-08-15,2026-09-15,USD,100,2026-08-15,2026-09-15\n",
         "billing_period_start_not_the_first_of_a_month"),
        ("claude,Purchase,2026-08-01,2026-09-01,USD,100,2026-08-01,\n",
         "billing_period_partially_supplied"),
        ("claude,Purchase,2026-08-01,2026-09-01,USD,100,2026-09-01,2026-08-01\n",
         "billing_period_end_not_after_start"),
        ("claude,Purchase,2026-08-01,2026-09-01,USD,100,2026-07-01,2026-08-01\n",
         "charge_period_start_outside_billing_period"),
    ]
    for row, reason in cases:
        report = invoice_ingest.ingest_csv(header + row)
        assert report["written"] is False, row
        assert report["rejections"][0]["reason"] == reason, row
    assert _rows() == []


# --- deliverable 2: reconciliation ------------------------------------------------


_COMPLETE_INVOICE = (
    "service_provider_name,charge_category,charge_period_start,charge_period_end,"
    "billing_currency,billed_cost\n"
    "claude,Purchase,2026-08-01,2026-09-01,USD,112.50\n"
)


def _telemetry_month(activity_db):
    """A provider-month this machine derived, balanced against plans.toml."""
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    result = focus.amortise_period("claude", "2026-08-01", plans=_PLANS)
    assert result["balanced"] is True
    return result


def test_the_difference_between_what_was_billed_and_what_was_derived(activity_db):
    """The number this deliverable exists to produce, on seeded rows for one month."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    report = invoice_ingest.reconcile_period("2026-08")

    provider = report["providers"][0]
    assert provider["service_provider_name"] == "claude"
    comparison = provider["comparisons"][0]
    assert comparison["x_source"] == focus.SOURCE_INVOICE
    assert comparison["compared_against"] == focus.SOURCE_TELEMETRY
    # The telemetry side totals the user's own plans.toml cost exactly once - item 2's
    # invariant, read back here rather than recomputed.
    assert comparison["telemetry_total"] == pytest.approx(100.0)
    assert comparison["source_total"] == pytest.approx(112.50)
    assert comparison["comparable"] is True
    assert comparison["difference"] == pytest.approx(12.50)
    assert comparison["difference_absent_reason"] is None


def test_each_side_reports_which_cost_column_it_was_totalled_on(activity_db):
    """The two sides total different columns, so the payload says which, not the reader."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    sides = {side["x_source"]: side
             for side in invoice_ingest.reconcile_period("2026-08")["providers"][0]["sides"]}
    assert sides[focus.SOURCE_TELEMETRY]["cost_column"] == "effective_cost"
    assert sides[focus.SOURCE_INVOICE]["cost_column"] == "billed_cost"


def test_an_invoice_that_matches_reports_zero_which_is_a_result_not_an_absence(activity_db):
    """0.0 means the sides agreed. None means we could not tell. Never conflated."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00\n"
    )

    comparison = invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"][0]
    assert comparison["comparable"] is True
    assert comparison["difference"] == 0.0
    assert comparison["difference"] is not None


def test_an_incomplete_invoice_side_refuses_to_report_a_difference(activity_db):
    """Subtracting a partial total from a whole one manufactures a discrepancy."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_fixture_text())  # its Usage line carries no amount

    comparison = invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"][0]
    assert comparison["comparable"] is False
    assert comparison["difference"] is None
    assert comparison["difference_absent_reason"] == (
        "invoice_side_incomplete:1_rows_carry_no_billed_cost"
    )
    # The partial totals are still shown, with the rows behind them counted, so a reader
    # can see how much of the side is actually there.
    assert comparison["source_total"] == pytest.approx(100.0)
    sides = {s["x_source"]: s
             for s in invoice_ingest.reconcile_period("2026-08")["providers"][0]["sides"]}
    assert sides[focus.SOURCE_INVOICE]["rows_with_cost"] == 2
    assert sides[focus.SOURCE_INVOICE]["rows_without_cost"] == 1


def test_an_incomplete_telemetry_side_refuses_too_and_names_that_side(activity_db):
    """Symmetry matters: the invoice is not assumed to be the untrustworthy side."""
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    # No weekly cap, so the Usage row is left absent rather than amortised.
    focus.amortise_period(
        "claude", "2026-08-01", plans={"claude": {"cost_usd_month": 100.0}}
    )
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    comparison = invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"][0]
    assert comparison["comparable"] is False
    assert comparison["difference"] is None
    assert comparison["difference_absent_reason"] == (
        "telemetry_side_incomplete:1_rows_carry_no_effective_cost"
    )


def test_an_invoice_for_a_provider_with_no_telemetry_says_so(activity_db):
    """A vendor surface that never reported leaves nothing to compare against."""
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    comparison = invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"][0]
    assert comparison["comparable"] is False
    assert comparison["telemetry_total"] is None
    assert comparison["difference_absent_reason"] == (
        "no_telemetry_rows_for_this_provider_period"
    )


def test_a_matching_invoice_reports_no_category_discrepancy_either(activity_db):
    """The false discrepancy this refuses to produce, on the month that exposes it.

    The vendor billed EXACTLY the configured cost, so the overall difference is 0.0. The
    earlier draft nevertheless reported a Purchase "difference" of 0.0392609699769082 -
    which is not a discrepancy at all, it is the amortised usage share. Telemetry
    Purchase.effective_cost is the RESIDUAL left after allocation; an invoice Purchase
    line is the whole billed subscription. The sum over categories is comparable because
    the allocation is internal to the month; a single category is not.
    """
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00\n"
    )

    comparison = invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"][0]
    assert comparison["difference"] == 0.0, "the bill matched exactly"

    by_category = {row["charge_category"]: row for row in comparison["by_charge_category"]}
    purchase = by_category["Purchase"]
    # The two side totals ARE reported - they are useful - and they differ, precisely
    # because they are not the same kind of quantity.
    assert purchase["source_total"] == pytest.approx(100.0)
    assert purchase["telemetry_total"] == pytest.approx(100.0 - 0.039260969976905306)
    assert purchase["telemetry_total"] != purchase["source_total"]
    # And no subtraction is offered, on ANY category, with the reason naming why.
    for row in comparison["by_charge_category"]:
        assert row["difference"] is None, row["charge_category"]
        assert row["difference_absent_reason"] == (
            "unlike_bases:telemetry_effective_cost_is_an_amortised_allocation_"
            "not_a_billed_line"
        )


def test_a_category_holding_one_costed_and_one_uncosted_row_says_so(activity_db):
    """The sum alone hid it: the costed figure was reported and the gap disappeared."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,x_model\n"
        "claude,Usage,2026-08-01,2026-09-01,USD,7.00,model-a\n"
        "claude,Usage,2026-08-02,2026-09-01,USD,,model-b\n"
    )

    sides = {s["x_source"]: s
             for s in invoice_ingest.reconcile_period("2026-08")["providers"][0]["sides"]}
    usage = [row for row in sides[focus.SOURCE_INVOICE]["by_charge_category"]
             if row["charge_category"] == "Usage"][0]
    assert usage["total"] == pytest.approx(7.0)
    assert usage["rows"] == 2
    assert usage["rows_with_cost"] == 1
    assert usage["rows_without_cost"] == 1


def test_a_row_with_unrecognised_provenance_is_excluded_and_announced(activity_db):
    """Not reconcilable, and not disappeared either. The table has no CHECK constraint."""
    _telemetry_month(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO focus_facts(service_provider_name, service_name,
                   charge_category, charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end, billed_cost, x_source,
                   created_at)
               VALUES('claude','Claude Max','Purchase','2026-08-01','2026-09-01',
                      '2026-08-01','2026-09-01', 55.0, 'hand_inserted', 9)"""
        )

    report = invoice_ingest.reconcile_period("2026-08")
    provider = report["providers"][0]

    assert report["providers_with_excluded_rows"] == ["claude"]
    assert provider["excluded_rows"] == [
        {"x_source": "hand_inserted", "rows": 1,
         "reason": "x_source_is_not_a_source_this_module_can_reconcile"}
    ]
    # And it joined no total on either side.
    sides = {s["x_source"]: s for s in provider["sides"]}
    assert "hand_inserted" not in sides
    assert sides[focus.SOURCE_TELEMETRY]["total"] == pytest.approx(100.0)


def test_a_customer_export_is_reconciled_as_its_own_side(activity_db):
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,105.00\n",
        source=focus.SOURCE_CUSTOMER_EXPORT,
    )

    comparisons = {c["x_source"]: c for c in
                   invoice_ingest.reconcile_period("2026-08")["providers"][0]["comparisons"]}
    assert comparisons[focus.SOURCE_INVOICE]["difference"] == pytest.approx(12.50)
    assert comparisons[focus.SOURCE_CUSTOMER_EXPORT]["difference"] == pytest.approx(5.0)


def test_reconciling_one_provider_does_not_read_another(activity_db):
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "codex,Purchase,2026-08-01,2026-09-01,USD,20.00\n"
    )

    scoped = invoice_ingest.reconcile_period("2026-08", provider="claude")
    assert [p["service_provider_name"] for p in scoped["providers"]] == ["claude"]
    both = invoice_ingest.reconcile_period("2026-08")
    assert [p["service_provider_name"] for p in both["providers"]] == ["claude", "codex"]


def test_a_period_that_is_not_a_month_is_rejected_rather_than_reconciled_empty(activity_db):
    focus.init()
    for period in ("2026-8", "2026-13", "august", ""):
        with pytest.raises(ValueError):
            invoice_ingest.reconcile_period(period)


# --- deliverable 3: coverage falls, and says why ----------------------------------


def _july_telemetry_and_invoice(activity_db):
    """A month where BOTH supply routes reported: the baseline a closure is measured from."""
    _seed_daily_model(activity_db, day="2026-07-15")
    focus.emit_usage_rows("claude", "2026-07-15", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-07-15", plans=_PLANS)
    focus.amortise_period("claude", "2026-07-01", plans=_PLANS)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-07-01,2026-08-01,USD,100.00\n"
    )


def test_coverage_falls_and_says_why_when_a_provider_surface_closes(activity_db):
    """The deliverable, demonstrated: telemetry stops, the invoice keeps arriving."""
    _july_telemetry_and_invoice(activity_db)
    # August: the provider surface has closed. No telemetry row is emitted at all.
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    august = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]

    assert august["sources_expected"] == [focus.SOURCE_INVOICE, focus.SOURCE_TELEMETRY]
    assert august["sources_present"] == [focus.SOURCE_INVOICE]
    assert august["sources_absent"] == [focus.SOURCE_TELEMETRY]
    assert august["source_coverage_pct"] == 50.0
    assert august["degraded"] is True
    assert "source_absent:telemetry" in august["coverage_reasons"]

    july = invoice_ingest.reconcile_period("2026-07")["providers"][0]["coverage"]
    assert july["source_coverage_pct"] == 100.0
    assert july["degraded"] is False
    assert july["coverage_reasons"] == []


def test_a_row_based_coverage_would_have_stayed_at_100_which_is_why_history_is_the_denominator(
    activity_db,
):
    """The trap this deliverable exists to avoid, shown rather than described.

    When a surface closes, its rows stop arriving - so a percentage taken over the rows
    that DID arrive loses numerator and denominator together and never moves. Both
    numbers are computed here on the same degraded month: the row-based one reads a
    reassuring 100%, and only the history-based one falls.
    """
    _july_telemetry_and_invoice(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]

    assert coverage["pricing_coverage_pct"] == 100.0, "every row that arrived carries a cost"
    assert coverage["pricing_coverage_denominator"] == "rows_present_in_this_period"
    assert coverage["source_coverage_pct"] == 50.0, "and yet half the supply is gone"
    assert coverage["source_coverage_denominator"] == (
        "sources_seen_on_or_before_this_period"
    )


def test_a_provider_that_went_completely_dark_still_appears_in_the_report(activity_db):
    """Disappearing from the report is how a coverage collapse hides."""
    _july_telemetry_and_invoice(activity_db)

    august = invoice_ingest.reconcile_period("2026-08")
    entry = august["providers"][0]

    assert entry["service_provider_name"] == "claude"
    assert entry["sides"] == []
    assert entry["coverage"]["source_coverage_pct"] == 0.0
    assert sorted(entry["coverage"]["sources_absent"]) == [
        focus.SOURCE_INVOICE, focus.SOURCE_TELEMETRY
    ]
    assert entry["coverage"]["degraded"] is True
    assert august["degraded_providers"] == ["claude"]
    # No rows at all is "we cannot say", not "nothing is covered".
    assert entry["coverage"]["pricing_coverage_pct"] is None
    assert entry["coverage"]["rows"] == 0


def test_a_provider_that_only_ever_sent_invoices_is_not_reported_as_degraded(activity_db):
    """Nothing went dark; a surface that never reported has not closed."""
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]
    assert coverage["sources_expected"] == [focus.SOURCE_INVOICE]
    assert coverage["source_coverage_pct"] == 100.0
    assert coverage["degraded"] is False


def test_coverage_names_the_rows_reason_for_carrying_no_cost(activity_db):
    """Reuses x_cost_basis rather than inventing a second vocabulary for the same fact."""
    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_fixture_text())  # its Usage line carries no amount

    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]
    assert coverage["pricing_coverage_pct"] == 80.0, "4 of 5 rows carry a cost"
    assert coverage["rows"] == 5
    assert coverage["rows_with_cost"] == 4
    assert coverage["unpriced_reasons"] == [
        "invoice:billed_cost_absent+effective_cost_absent"
    ]
    assert "invoice:billed_cost_absent+effective_cost_absent" in coverage["coverage_reasons"]


def test_the_sources_a_provider_is_expected_to_have_are_derived_not_listed(activity_db):
    """A hand-written expectation would go stale the first time a provider was added."""
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "a-brand-new-vendor,Purchase,2026-08-01,2026-09-01,USD,5.00\n"
    )
    coverage = invoice_ingest.reconcile_period(
        "2026-08", provider="a-brand-new-vendor"
    )["providers"][0]["coverage"]
    assert coverage["sources_expected"] == [focus.SOURCE_INVOICE]
    assert coverage["degraded"] is False


def test_a_degraded_month_is_flagged_at_the_top_of_the_report(activity_db):
    """A reader scanning the payload must not have to walk every provider to notice."""
    _july_telemetry_and_invoice(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "codex,Purchase,2026-08-01,2026-09-01,USD,20.00\n"
    )

    report = invoice_ingest.reconcile_period("2026-08")
    assert report["degraded_providers"] == ["claude"]
    assert [p["service_provider_name"] for p in report["providers"]] == ["claude", "codex"]


# --- correction: the four input-integrity blockers, each as a regression -----------


def test_an_importer_may_not_stamp_a_file_as_telemetry(activity_db):
    """The worst of the four: it fabricated provenance AND destroyed the real rows.

    `ingest_csv` deletes the rows matching the source it writes. Stamping an import
    `telemetry` therefore deleted the provider-month's actual telemetry and replaced it
    with invoice-shaped rows indistinguishable from measurements - the precise
    delete-then-insert destruction the source identity exists to remove, handed back to
    the caller through the front door.
    """
    _seed_daily_model(activity_db)
    focus.emit_usage_rows("claude", "2026-08-27", plans=_PLANS)
    focus.emit_purchase_row("claude", "2026-08-27", plans=_PLANS)
    before = [(r["charge_category"], r["billed_cost"], r["x_model"]) for r in _rows()]
    assert before, "the telemetry this must protect has to exist first"

    with pytest.raises(invoice_ingest.IngestError, match="may not be written by an importer"):
        invoice_ingest.ingest_csv(_fixture_text(), source=focus.SOURCE_TELEMETRY)

    after = [(r["charge_category"], r["billed_cost"], r["x_model"]) for r in _rows()]
    assert after == before, "no telemetry row may be touched by a refused import"
    assert {r["x_source"] for r in _rows()} == {focus.SOURCE_TELEMETRY}


def test_only_the_two_importable_sources_are_writable_by_an_importer(activity_db):
    """Derived from the module's own allowlist, not from a copy of it."""
    assert invoice_ingest.INGESTIBLE_SOURCES == {
        focus.SOURCE_INVOICE, focus.SOURCE_CUSTOMER_EXPORT
    }
    assert focus.SOURCE_TELEMETRY not in invoice_ingest.INGESTIBLE_SOURCES
    assert invoice_ingest.INGESTIBLE_SOURCES < focus.SOURCES
    for source in sorted(invoice_ingest.INGESTIBLE_SOURCES):
        assert invoice_ingest.ingest_csv(_fixture_text(), source=source)["written"] is True


def test_a_non_finite_number_is_refused_rather_than_stored(activity_db):
    """NaN becomes SQLite NULL, so an invalid input would come back as an ABSENCE."""
    header = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
    )
    for literal in ("nan", "NaN", "inf", "-inf", "Infinity"):
        report = invoice_ingest.ingest_csv(
            header + f"claude,Purchase,2026-08-01,2026-09-01,USD,{literal}\n"
        )
        assert report["written"] is False, literal
        assert report["rejections"][0]["reason"] == "billed_cost_not_finite", literal
        assert report["rejections"][0]["detail"] == literal
    assert _rows() == [], "an unstorable number must not reach the table at all"


def test_a_negative_cost_is_kept_and_a_negative_quantity_is_refused(activity_db):
    """A credit is a real invoice line. Minus one seat-month is a malformed input."""
    report = invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Credit,2026-08-01,2026-09-01,USD,-15.00\n"
    )
    assert report["written"] is True
    assert _rows()[0]["billed_cost"] == -15.0

    refused = invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,consumed_quantity,consumed_unit\n"
        "claude,Usage,2026-08-01,2026-09-01,USD,5.00,-3,tokens\n"
    )
    assert refused["written"] is False
    assert refused["rejections"][0]["reason"] == "consumed_quantity_negative"


def test_a_repeated_column_is_refused_rather_than_silently_last_wins(activity_db):
    """csv.DictReader keeps the LAST value, so the first figure vanishes without a word."""
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00,999.00\n"
    )
    with pytest.raises(invoice_ingest.IngestError, match="repeats column"):
        invoice_ingest.ingest_csv(text)
    assert _rows() == []


def test_a_row_whose_cell_count_disagrees_with_the_header_is_refused(activity_db):
    """Structural, and distinct from an empty field, which is a legitimate absence."""
    header = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
    )
    extra = invoice_ingest.ingest_csv(
        header + "claude,Purchase,2026-08-01,2026-09-01,USD,100.00,STRAY\n")
    assert extra["written"] is False
    assert extra["rejections"][0]["reason"] == "row_has_more_cells_than_the_header"

    short = invoice_ingest.ingest_csv(header + "claude,Purchase,2026-08-01\n")
    assert short["written"] is False
    assert short["rejections"][0]["reason"] == "row_has_fewer_cells_than_the_header"

    # An EMPTY cell is not a structural error. It is the absence the contract defines.
    empty = invoice_ingest.ingest_csv(
        header + "claude,Purchase,2026-08-01,2026-09-01,USD,\n")
    assert empty["written"] is True
    assert _rows()[0]["billed_cost"] is None


def test_two_lines_sharing_a_row_identity_are_named_not_raised_from_sqlite(activity_db):
    """A raw IntegrityError from inside the transaction names no line and no file."""
    text = (
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,100.00\n"
        "claude,Purchase,2026-08-01,2026-09-01,USD,120.00\n"
    )
    report = invoice_ingest.ingest_csv(text)
    assert report["written"] is False
    assert report["rejections"][0]["reason"] == "duplicate_row_identity_in_file"
    assert report["rejections"][0]["line"] == 3
    assert "line 2 already carries" in report["rejections"][0]["detail"]
    assert _rows() == []


def test_the_import_contract_still_only_uses_columns_the_export_emits(activity_db):
    """Re-asserted after the contract gained two columns in the correction."""
    exported = {column for column, _ in focus.FOCUS_CSV_COLUMNS}
    contract = set(invoice_ingest.REQUIRED_COLUMNS) | set(invoice_ingest.OPTIONAL_COLUMNS)
    assert contract <= exported, sorted(contract - exported)
    assert "billing_period_start" in contract and "billing_period_end" in contract


# --- correction: the deliverable-3 blockers -----------------------------------------


def test_onboarding_a_vendor_does_not_retroactively_degrade_an_earlier_month(activity_db):
    """A historical report must be stable: tomorrow may not edit last month's answer.

    Unbounded history reached into the FUTURE. A vendor first seen in August made July
    report it as a provider whose supply had collapsed to 0%, naming a source that did
    not exist yet.
    """
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost\n"
        "future-vendor,Purchase,2026-08-01,2026-09-01,USD,5.00\n"
    )

    july = invoice_ingest.reconcile_period("2026-07")
    assert july["providers"] == []
    assert july["degraded_providers"] == []

    august = invoice_ingest.reconcile_period("2026-08")
    assert [p["service_provider_name"] for p in august["providers"]] == ["future-vendor"]
    assert august["degraded_providers"] == [], "arriving is not degrading"


def test_a_source_that_stops_still_degrades_the_month_after_it_stopped(activity_db):
    """Bounding history must not blunt the signal it was bounded to keep honest."""
    _july_telemetry_and_invoice(activity_db)
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    august = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]
    assert august["sources_expected"] == [focus.SOURCE_INVOICE, focus.SOURCE_TELEMETRY]
    assert august["source_coverage_pct"] == 50.0
    assert august["degraded"] is True


def test_a_customer_export_row_does_not_call_itself_an_invoice(activity_db):
    """A row disagreeing with its own x_source is a provenance conflict inside one row."""
    invoice_ingest.ingest_csv(_fixture_text(), source=focus.SOURCE_CUSTOMER_EXPORT)

    for row in _rows():
        assert row["x_source"] == focus.SOURCE_CUSTOMER_EXPORT
        assert row["x_cost_basis"].startswith("customer_export:")
        assert "invoice:" not in row["x_cost_basis"]


def test_the_cost_basis_names_all_four_supplied_and_absent_combinations(activity_db):
    """Absent is a fact about the input, recorded as one, never inferred from a NULL."""
    invoice_ingest.ingest_csv(
        "service_provider_name,charge_category,charge_period_start,charge_period_end,"
        "billing_currency,billed_cost,effective_cost,x_model\n"
        "claude,Usage,2026-08-01,2026-09-01,USD,1.00,2.00,both\n"
        "claude,Usage,2026-08-02,2026-09-01,USD,1.00,,billed-only\n"
        "claude,Usage,2026-08-03,2026-09-01,USD,,2.00,effective-only\n"
        "claude,Usage,2026-08-04,2026-09-01,USD,,,neither\n"
    )

    basis = {row["x_model"]: row["x_cost_basis"] for row in _rows()}
    assert basis == {
        "both": "invoice:billed_cost_supplied+effective_cost_supplied",
        "billed-only": "invoice:billed_cost_supplied+effective_cost_absent",
        "effective-only": "invoice:billed_cost_absent+effective_cost_supplied",
        "neither": "invoice:billed_cost_absent+effective_cost_absent",
    }


def test_row_pricing_coverage_keeps_the_name_this_repo_already_uses(activity_db):
    """Same concept, same vocabulary. A second name would be the parallel concept."""
    from src import cost_estimates, work_reporting  # noqa: F401  - proves the name is theirs

    _telemetry_month(activity_db)
    invoice_ingest.ingest_csv(_fixture_text())
    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]

    assert "pricing_coverage_pct" in coverage
    assert "cost_coverage_pct" not in coverage
    assert "unpriced_reasons" in coverage
    # And the denominator is stated, because it is rows here and tokens there.
    assert coverage["pricing_coverage_denominator"] == "rows_present_in_this_period"


# --- integration correction: the three defects across the D2 and D3 corrections ------


def test_an_empty_source_is_not_reported_as_something_this_machine_measured(activity_db):
    """`or` coerced '' to telemetry; the index's COALESCE does not. Readers must match it.

    The coercion also ran BEFORE the RECONCILED_COLUMN check, so the excluded_rows guard
    built for exactly this class of row never fired.
    """
    _telemetry_month(activity_db)
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO focus_facts(service_provider_name, service_name,
                   charge_category, charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end, billed_cost, effective_cost,
                   x_model, x_source, created_at)
               VALUES('claude','Claude Max','Usage','2026-08-01','2026-09-01',
                      '2026-08-01','2026-09-01', 11.0, 11.0, 'empty-source', '', 9)"""
        )

    summary = focus.summary_for_period("2026-08", plans=_PLANS)["providers"][0]
    by_source = {block["x_source"]: block for block in summary["by_source"]}
    assert "" in by_source, "an empty source is its own provenance, not telemetry"
    assert by_source[""]["rows"] == 1
    assert by_source[focus.SOURCE_TELEMETRY]["rows"] == 2, "the real telemetry, unblended"
    assert summary["effective_cost_total"] == pytest.approx(100.0), "11.0 must not join it"

    provider = invoice_ingest.reconcile_period("2026-08")["providers"][0]
    assert provider["excluded_rows"] == [
        {"x_source": "", "rows": 1,
         "reason": "x_source_is_not_a_source_this_module_can_reconcile"}
    ]
    sides = {side["x_source"]: side for side in provider["sides"]}
    assert sides[focus.SOURCE_TELEMETRY]["total"] == pytest.approx(100.0)


def test_a_null_source_is_still_read_as_telemetry(activity_db):
    """The NULL case is intended and consistent: the index COALESCEs it the same way."""
    focus.init()
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO focus_facts(service_provider_name, service_name,
                   charge_category, charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end, billed_cost, effective_cost,
                   x_source, created_at)
               VALUES('claude','Claude Max','Usage','2026-08-01','2026-09-01',
                      '2026-08-01','2026-09-01', 22.0, 22.0, NULL, 9)"""
        )

    summary = focus.summary_for_period("2026-08", plans=_PLANS)["providers"][0]
    assert [b["x_source"] for b in summary["by_source"]] == [focus.SOURCE_TELEMETRY]
    assert summary["effective_cost_total"] == pytest.approx(22.0)

    # BOTH readers, because both were changed. summary_for_period above is only half of
    # it: reconcile_period is a second COALESCE site, and a NULL that stopped being read
    # as telemetry there would fall out of `sides` into `excluded_rows` unnoticed.
    provider = invoice_ingest.reconcile_period("2026-08")["providers"][0]
    assert [side["x_source"] for side in provider["sides"]] == [focus.SOURCE_TELEMETRY]
    sides = {side["x_source"]: side for side in provider["sides"]}
    assert sides[focus.SOURCE_TELEMETRY]["total"] == pytest.approx(22.0)
    assert provider["excluded_rows"] == []


def test_an_announced_excluded_row_does_not_also_report_its_source_absent(activity_db):
    """The same payload said the row was present AND that its source had disappeared.

    A false degradation, in the report built to announce real ones. `_sources_seen_by`
    admitted every raw source to the denominator while `sides` admitted only the
    reconcilable ones, so a present-but-excluded source was classified as a disappearance.
    """
    focus.init()
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO focus_facts(service_provider_name, service_name,
                   charge_category, charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end, billed_cost, x_source,
                   created_at)
               VALUES('claude','Claude Max','Purchase','2026-08-01','2026-09-01',
                      '2026-08-01','2026-09-01', 55.0, 'hand_inserted', 9)"""
        )

    report = invoice_ingest.reconcile_period("2026-08")
    provider = report["providers"][0]

    # Announced as PRESENT...
    assert provider["excluded_rows"][0]["x_source"] == "hand_inserted"
    assert report["providers_with_excluded_rows"] == ["claude"]
    # ...and therefore never also reported as absent.
    coverage = provider["coverage"]
    assert coverage["sources_expected"] == []
    assert coverage["sources_absent"] == []
    assert coverage["degraded"] is False
    assert coverage["coverage_reasons"] == []
    assert report["degraded_providers"] == []
    # No reconcilable source at all is "we cannot say", not "nothing is covered".
    assert coverage["source_coverage_pct"] is None


def test_an_unknown_source_in_an_earlier_period_never_becomes_expected(activity_db):
    """Otherwise one stray row makes every later month permanently degraded."""
    focus.init()
    with sqlite3.connect(activity_db) as conn:
        conn.execute(
            """INSERT INTO focus_facts(service_provider_name, service_name,
                   charge_category, charge_period_start, charge_period_end,
                   billing_period_start, billing_period_end, billed_cost, x_source,
                   created_at)
               VALUES('claude','Claude Max','Purchase','2026-07-01','2026-08-01',
                      '2026-07-01','2026-08-01', 55.0, 'hand_inserted', 9)"""
        )
    invoice_ingest.ingest_csv(_COMPLETE_INVOICE)

    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]
    assert coverage["sources_expected"] == [focus.SOURCE_INVOICE]
    assert coverage["degraded"] is False


def test_the_denominator_label_describes_the_query_that_produces_it(activity_db):
    """The query became "on or before this period" while the label still said "ever"."""
    _telemetry_month(activity_db)
    coverage = invoice_ingest.reconcile_period("2026-08")["providers"][0]["coverage"]
    assert coverage["source_coverage_denominator"] == (
        "sources_seen_on_or_before_this_period"
    )
