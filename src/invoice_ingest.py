"""Ingest provider invoices and customer-supplied billing exports into `focus_facts`.

This is the SECOND supply route. Every other path into this table starts at a provider
surface this machine reads; when a vendor narrows, reprices, or closes that surface, the
number goes dark. An invoice is a record the user already holds, so it keeps arriving
after the surface stops answering.

An invoice-derived row is NOT a better copy of a telemetry-derived one and never replaces
it. Both land, distinguished by `x_source`, so the two can be compared - which is the only
reason to have both.

WHAT THIS MODULE WILL AND WILL NOT DO WITH A NUMBER
- A cost the input leaves EMPTY is recorded ABSENT (NULL). It is not read as zero.
- A cost the input states as `0` IS a measurement and is recorded as 0.0. A vendor line
  item that really was free is a fact; refusing it would be as dishonest as inventing one.
  Emptiness and zero are therefore different inputs here, deliberately.
- Nothing is derived. `billed_cost` is what the input said. `effective_cost` is filled only
  when the input supplies it, because amortising a vendor's figure against this machine's
  plan configuration would replace a measured number with a computed one.

THE INPUT SHAPE IS OURS, NOT A VENDOR'S. No real vendor invoice export was available to
this workspace to model, so this is a documented import contract of our own rather than a
mirror of anybody's format, and it says so rather than implying a fidelity it does not
have. See `tests/fixtures/invoices/SOURCE.md`.
"""

from __future__ import annotations

import csv
import io
import math
from contextlib import closing
from datetime import date

from src import focus, install_identity

# The import contract. A column outside these two sets is a contract violation rather
# than something to guess at: a header this code does not understand usually means the
# file is a different format, and silently ingesting the columns it recognised would
# produce a confidently partial month.
REQUIRED_COLUMNS = (
    "service_provider_name",
    "charge_category",
    "charge_period_start",
    "charge_period_end",
    "billing_currency",
    "billed_cost",
)
OPTIONAL_COLUMNS = (
    "service_name",
    "x_model",
    "pricing_quantity",
    "pricing_unit",
    "consumed_quantity",
    "consumed_unit",
    "effective_cost",
    "billing_period_start",
    "billing_period_end",
)

# The sources this module may WRITE. `telemetry` is deliberately absent, and its absence
# is the point: telemetry means "this machine observed it", so letting an importer stamp
# a file with it would both fabricate provenance and hand the importer the exact
# delete-then-insert path that item 4 exists to take away. `ingest_csv` deletes the rows
# matching the source it is writing, so a telemetry-stamped import would delete the
# provider-month's real telemetry rows and replace them with invoice-shaped rows
# indistinguishable from measurements. Refused at parse time, before any write.
INGESTIBLE_SOURCES = frozenset({focus.SOURCE_INVOICE, focus.SOURCE_CUSTOMER_EXPORT})

# Only USD is accepted. Nothing in this repo converts currency, and `summary_for_period`
# adds `billed_cost` values together; letting a EUR line in would produce a total in no
# currency at all, which is worse than refusing the row and saying why.
SUPPORTED_CURRENCY = "USD"


class IngestError(ValueError):
    """A file this module refuses to ingest at all, with the reason in the message."""


def _rejection(line: int, reason: str, detail: str = "") -> dict:
    return {"line": line, "reason": reason, "detail": detail}


def _parse_number(
    raw: str | None, line: int, column: str, rejections: list, *, allow_negative: bool
) -> float | None:
    """An empty field is ABSENT; a stated number is a measurement; anything else fails.

    `nan` and `inf` are refused rather than stored. Both parse happily through `float()`,
    and both are worse than a rejection: SQLite turns NaN into NULL, so an input stating
    something invalid would come back as the ABSENCE this table reserves for "never
    measured", and an infinity poisons every total it is added into.

    Negative COSTS are allowed - a credit or a refund is a real invoice line, and
    refusing one would drop money out of the reconciliation. Negative QUANTITIES are
    refused: nothing consumes a negative number of tokens or holds minus one seat, so a
    negative there is a malformed input rather than a fact about the month.
    """
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        rejections.append(_rejection(line, f"{column}_not_a_number", raw.strip()))
        return None
    if not math.isfinite(value):
        rejections.append(_rejection(line, f"{column}_not_finite", raw.strip()))
        return None
    if value < 0 and not allow_negative:
        rejections.append(_rejection(line, f"{column}_negative", raw.strip()))
        return None
    return value


def _parse_date(raw: str | None, line: int, column: str, rejections: list) -> date | None:
    """A real calendar date, not a string that merely looks like one.

    A shape check passes `2026-02-31`, which is not a day. Ingested, it would sit in the
    table as a charge period no real-month query could ever match, so the cost it carries
    would vanish from every report while the row itself looked perfectly healthy.
    """
    text = (raw or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        rejections.append(_rejection(line, f"{column}_not_a_calendar_date", text))
        return None


def parse_csv(text: str, *, source: str = focus.SOURCE_INVOICE) -> dict:
    """Parse the import contract into rows ready to write, without touching the database.

    Returns `{"source", "rows", "rejections"}`. Parsing is separated from writing so that
    a file with a single bad line can be reported in full before anything is committed;
    `ingest_csv` refuses to write when `rejections` is non-empty.
    """
    source = focus._checked_source(source)
    if source not in INGESTIBLE_SOURCES:
        raise IngestError(
            f"x_source {source!r} may not be written by an importer; a file may be "
            f"ingested only as {sorted(INGESTIBLE_SOURCES)}. {focus.SOURCE_TELEMETRY!r} "
            "means this machine observed the row, so stamping an import with it would "
            "both fabricate provenance and delete the provider-month's real telemetry."
        )
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        # csv.DictReader keeps the LAST value for a repeated name, so a file carrying
        # `billed_cost` twice silently discards the first figure. Refused rather than
        # resolved: which of the two the vendor meant is not ours to decide.
        raise IngestError(
            f"input repeats column(s) {duplicates}; each column may appear once"
        )
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise IngestError(
            f"input is missing required column(s) {missing}; the contract requires "
            f"{list(REQUIRED_COLUMNS)}"
        )
    unknown = [name for name in header if name not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    if unknown:
        raise IngestError(
            f"input carries column(s) {unknown} that this contract does not define; "
            f"known columns are {list(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)}"
        )

    rows: list[dict] = []
    rejections: list[dict] = []
    seen_identities: dict[tuple, int] = {}
    for offset, raw in enumerate(reader):
        line = offset + 2  # 1-based, and the header occupies line 1

        # A row with MORE cells than the header lands under DictReader's restkey, and a
        # row with fewer leaves header names mapped to None. Both are structural, and
        # both are distinct from an empty field, which is a legitimate ABSENCE. Guessing
        # which column a stray cell belonged to would be inventing the input.
        if None in raw:
            rejections.append(_rejection(
                line, "row_has_more_cells_than_the_header", str(raw[None])))
            continue
        short = [name for name in header if raw.get(name) is None]
        if short:
            rejections.append(_rejection(
                line, "row_has_fewer_cells_than_the_header", str(short)))
            continue

        provider = raw["service_provider_name"].strip()
        if not provider:
            rejections.append(_rejection(line, "service_provider_name_empty"))
            continue

        category = raw["charge_category"].strip()
        if category not in focus.CHARGE_CATEGORIES:
            rejections.append(_rejection(line, "charge_category_not_a_focus_category", category))
            continue

        before = len(rejections)
        start = _parse_date(raw["charge_period_start"], line, "charge_period_start", rejections)
        end = _parse_date(raw["charge_period_end"], line, "charge_period_end", rejections)
        if len(rejections) != before:
            continue
        if end <= start:
            rejections.append(_rejection(
                line, "charge_period_end_not_after_start", f"{start}..{end}"))
            continue

        billing_start, billing_end, basis = _billing_period(raw, line, start, rejections)
        if billing_start is None:
            continue

        currency = raw["billing_currency"].strip().upper()
        if currency != SUPPORTED_CURRENCY:
            rejections.append(_rejection(line, "billing_currency_unsupported", currency))
            continue

        before = len(rejections)
        billed = _parse_number(
            raw.get("billed_cost"), line, "billed_cost", rejections, allow_negative=True)
        effective = _parse_number(
            raw.get("effective_cost"), line, "effective_cost", rejections, allow_negative=True)
        pricing_quantity = _parse_number(
            raw.get("pricing_quantity"), line, "pricing_quantity", rejections,
            allow_negative=False)
        consumed_quantity = _parse_number(
            raw.get("consumed_quantity"), line, "consumed_quantity", rejections,
            allow_negative=False)
        if len(rejections) != before:
            continue

        model = (raw.get("x_model") or "").strip() or None
        # The row identity the widened UNIQUE index enforces. Two lines sharing it would
        # reach SQLite as a raw IntegrityError from inside a transaction; named here
        # instead, beside the line number that caused it and every other rejection.
        identity = (provider, category, start.isoformat(), model or "")
        if identity in seen_identities:
            rejections.append(_rejection(
                line, "duplicate_row_identity_in_file",
                f"line {seen_identities[identity]} already carries "
                f"{provider}/{category}/{start.isoformat()}/{model or ''}"))
            continue
        seen_identities[identity] = line

        rows.append(
            {
                "service_provider_name": provider,
                "service_name": (raw.get("service_name") or "").strip() or provider,
                "charge_category": category,
                "charge_period_start": start.isoformat(),
                "charge_period_end": end.isoformat(),
                "billing_period_start": billing_start,
                "billing_period_end": billing_end,
                "pricing_quantity": pricing_quantity,
                "pricing_unit": (raw.get("pricing_unit") or "").strip() or None,
                "consumed_quantity": consumed_quantity,
                "consumed_unit": (raw.get("consumed_unit") or "").strip() or None,
                "billed_cost": billed,
                "effective_cost": effective,
                "billing_currency": currency,
                "x_model": model,
                "x_cost_basis": _cost_basis(source, billed, effective),
                "x_billing_period_basis": basis,
                "x_source": source,
                "line": line,
            }
        )
    return {"source": source, "rows": rows, "rejections": rejections}


def _cost_basis(source: str, billed: float | None, effective: float | None) -> str:
    """Say which figures the input actually supplied, for THIS source, without lying.

    Two earlier faults, both of them the row asserting something untrue about itself:
    every row was prefixed `invoice:` even when the source was `customer_export`, so a
    single row disagreed with its own `x_source`; and a row with NO billed cost still
    said `billed_cost_as_supplied`, which is the precise opposite of what happened. The
    coverage reasons repeat this string verbatim, so a basis that lies makes the report
    that explains a coverage gap explain it wrongly.

    All FOUR combinations are named. Absent is a fact about the input and is recorded as
    one; it is never left to be inferred from a NULL that could equally mean anything.
    """
    supplied = lambda value: "supplied" if value is not None else "absent"
    return f"{source}:billed_cost_{supplied(billed)}+effective_cost_{supplied(effective)}"


def _billing_period(
    raw: dict, line: int, charge_start: date, rejections: list
) -> tuple[str | None, str | None, str | None]:
    """The billing period, and an HONEST label for where it came from.

    Supplied or derived, and the row says which. An earlier draft labelled every row
    `source_supplied_period` while deriving the calendar month around the charge period,
    which was simply false: it claimed the vendor had stated a boundary the input had no
    column for. The label now tracks what actually happened.

    A supplied billing period must start on the first of a month. Every period query in
    this repo - `facts_for_period`, the export, the summary, the reconciliation - looks
    the row up by a `YYYY-MM-01` key, so a row starting mid-month on a billing
    anniversary would be unreachable by all of them and its cost would disappear from
    every report while the row itself sat in the table looking healthy. Refused and named
    rather than stored where nothing can find it.
    """
    supplied_start = (raw.get("billing_period_start") or "").strip()
    supplied_end = (raw.get("billing_period_end") or "").strip()
    if not supplied_start and not supplied_end:
        start, end = focus._month_bounds(charge_start.isoformat())
        return start, end, "derived_calendar_month_from_charge_period_start"
    if not supplied_start or not supplied_end:
        rejections.append(_rejection(
            line, "billing_period_partially_supplied",
            f"{supplied_start!r}..{supplied_end!r}"))
        return None, None, None

    before = len(rejections)
    start = _parse_date(supplied_start, line, "billing_period_start", rejections)
    end = _parse_date(supplied_end, line, "billing_period_end", rejections)
    if len(rejections) != before:
        return None, None, None
    if start.day != 1:
        rejections.append(_rejection(
            line, "billing_period_start_not_the_first_of_a_month", start.isoformat()))
        return None, None, None
    if end <= start:
        rejections.append(_rejection(
            line, "billing_period_end_not_after_start", f"{start}..{end}"))
        return None, None, None
    if not start <= charge_start < end:
        # A charge outside the billing period it claims is incoherent, and the row would
        # be filed under a month that does not contain it.
        rejections.append(_rejection(
            line, "charge_period_start_outside_billing_period",
            f"{charge_start} not in {start}..{end}"))
        return None, None, None
    return start.isoformat(), end.isoformat(), "source_supplied_period"


def ingest_csv(text: str, *, source: str = focus.SOURCE_INVOICE, now_us: int | None = None) -> dict:
    """Write an invoice or customer billing export into `focus_facts`. Fails closed.

    NOTHING IS WRITTEN IF ANY LINE IS REJECTED. A partially ingested invoice is the worst
    of the three outcomes: it looks like a complete month, totals like an incomplete one,
    and nothing in the table records which lines are missing. The caller gets every
    rejection at once instead, and the file is fixed and re-ingested.

    Idempotent per provider-month: re-ingesting replaces the rows this source previously
    wrote for each (provider, billing period) the file mentions, so a corrected invoice
    supersedes rather than duplicating. Telemetry-derived rows are never touched - the
    delete is scoped to this source, the mirror image of the telemetry-scoped deletes in
    `focus.emit_usage_rows` and `focus.emit_purchase_row`.

    A provider-month the file does not mention is left alone. Withdrawing a whole month
    means ingesting a file that still names it, the same limitation daily emission has.
    """
    focus.init()
    parsed = parse_csv(text, source=source)
    report = {
        "source": parsed["source"],
        "rows_ingested": 0,
        "rows_rejected": len(parsed["rejections"]),
        "rejections": parsed["rejections"],
        "provider_periods": [],
        "written": False,
    }
    if parsed["rejections"]:
        return report

    stamp = focus._now_us() if now_us is None else int(now_us)
    scopes = sorted({(row["service_provider_name"], row["billing_period_start"])
                     for row in parsed["rows"]})
    report["provider_periods"] = [
        {"service_provider_name": provider, "billing_period_start": period}
        for provider, period in scopes
    ]

    with closing(focus._connect()) as conn:
        for provider, period in scopes:
            conn.execute(
                """DELETE FROM focus_facts
                   WHERE service_provider_name = ? AND billing_period_start = ?
                     AND COALESCE(x_source, ?) = ?""",
                (provider, period, focus.SOURCE_TELEMETRY, parsed["source"]),
            )
        for row in parsed["rows"]:
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
                    row["service_provider_name"], row["service_name"],
                    focus._checked_category(row["charge_category"]),
                    row["charge_period_start"], row["charge_period_end"],
                    row["billing_period_start"], row["billing_period_end"],
                    row["pricing_quantity"], row["pricing_unit"],
                    row["consumed_quantity"], row["consumed_unit"],
                    row["billed_cost"], row["effective_cost"], row["billing_currency"],
                    row["service_provider_name"], None, row["x_model"],
                    # Token counts stay NULL. An invoice states money, not this machine's
                    # token accounting; a 0 here would be counted as observed consumption.
                    None, None, None,
                    None, None, None,
                    row["x_billing_period_basis"], row["x_cost_basis"],
                    # x_install_id records which install WROTE the row, uniformly with
                    # the telemetry writers. It is not a claim that this machine
                    # produced the vendor's figure - x_source already separates a
                    # billed number from a derived one. Leaving it to default would
                    # collapse "ingested here" and "predates the column" into one
                    # marker, and those are different facts.
                    focus._checked_source(row["x_source"]), install_identity.stamp(),
                    stamp,
                ),
            )
            report["rows_ingested"] += 1
        conn.commit()
    report["written"] = True
    return report


# The cost column each side is reconciled ON, and why they differ. Telemetry rows carry
# `effective_cost`, the amortised figure that totals the user's own plans.toml cost
# exactly once for a provider-month (item 2's invariant); their `billed_cost` is NULL on
# every Usage row, so totalling it would compare a month against a single line. Invoice
# rows carry `billed_cost`, which is what the vendor actually charged. The difference
# between the two is therefore a real question - what you were billed against what you
# believe you pay - and NOT an artefact of comparing unlike columns, which is why the
# column each side used is reported beside its total rather than left implicit.
RECONCILED_COLUMN = {
    focus.SOURCE_TELEMETRY: "effective_cost",
    focus.SOURCE_INVOICE: "billed_cost",
    focus.SOURCE_CUSTOMER_EXPORT: "billed_cost",
}


# Why a category-level SUBTRACTION is refused while the total-level one is not.
#
# Telemetry `effective_cost` is an ALLOCATION of one monthly bill across the rows of the
# month: each Usage row takes the share consumption used, and the Purchase row takes the
# residual capacity nobody used. The split between categories is internal bookkeeping, so
# the SUM over all categories equals the plan cost exactly once (item 2's invariant) and
# is a real quantity to compare against an invoice total. Any SINGLE category, though,
# carries an arbitrary slice of that allocation, and an invoice's Purchase line carries
# the whole billed subscription. Subtracting one from the other reports a discrepancy
# whose size is just the amortised usage share - so a month where the vendor billed
# EXACTLY the configured cost, whose overall difference is 0.0, would show a non-zero
# "Purchase discrepancy". A false discrepancy is worse than no number, so the per-side
# category totals are reported and the subtraction is not.
CATEGORY_DIFFERENCE_ABSENT_REASON = (
    "unlike_bases:telemetry_effective_cost_is_an_amortised_allocation_not_a_billed_line"
)


def _side(source: str, rows: list[dict]) -> dict:
    """One source's contribution to a provider-period, with what is missing counted."""
    column = RECONCILED_COLUMN[source]
    side = {
        "x_source": source,
        "cost_column": column,
        "rows": len(rows),
        "rows_with_cost": 0,
        "rows_without_cost": 0,
        "total": None,
        "uncosted_reasons": set(),
        "by_charge_category": {},
    }
    for row in rows:
        value = row[column]
        category = row["charge_category"]
        # Counted per category as well as per side. An earlier draft recorded only the
        # sum, so a category holding one costed row and one uncosted row reported the
        # costed figure alone and its incompleteness vanished.
        bucket = side["by_charge_category"].setdefault(
            category,
            {"charge_category": category, "total": None,
             "rows": 0, "rows_with_cost": 0, "rows_without_cost": 0},
        )
        bucket["rows"] += 1
        if value is None:
            bucket["rows_without_cost"] += 1
            side["rows_without_cost"] += 1
            # The row already records WHY it carries no cost. Reusing that string keeps
            # the explanation in one place instead of inventing a second vocabulary for
            # the same fact, and it is why a coverage number here can say why it fell.
            side["uncosted_reasons"].add(row["x_cost_basis"] or "cost_basis_unrecorded")
            continue
        bucket["rows_with_cost"] += 1
        bucket["total"] = (bucket["total"] or 0.0) + float(value)
        side["rows_with_cost"] += 1
        side["total"] = (side["total"] or 0.0) + float(value)
    side["uncosted_reasons"] = sorted(side["uncosted_reasons"])
    side["by_charge_category"] = [
        side["by_charge_category"][category]
        for category in sorted(side["by_charge_category"])
    ]
    return side


def reconcile_period(period: str, *, provider: str | None = None) -> dict:
    """Compare what a vendor billed against what this machine derived, per provider-month.

    THE DIFFERENCE IS REPORTED ONLY WHEN BOTH SIDES ARE WHOLE. A side with a row whose
    cost is absent has an incomplete total, and subtracting an incomplete total from a
    complete one produces a difference that reads as a discrepancy when it is really a
    gap in the input. Such a side is marked not comparable and the reason is named; the
    partial totals are still reported, with the rows behind them counted, so a reader can
    see how much of the side is actually there. This is the honest-denominator discipline
    item 2 established, applied to a subtraction instead of a sum.

    A difference of zero is a RESULT and is reported as 0.0. An unavailable difference is
    None. The two are never conflated: "the invoice matched" and "we could not tell"
    are opposite findings, and a 0.0 standing in for the second would read as the first.
    """
    billing_start = focus.period_start(period)
    rows = focus.facts_for_period(billing_start)
    if provider is not None:
        rows = [row for row in rows if row["service_provider_name"] == provider]

    by_provider: dict[str, dict[str, list[dict]]] = {}
    unknown: dict[str, dict[str, int]] = {}
    for row in rows:
        # None-only, never truthiness: see the matching note in focus.summary_for_period.
        # An empty-string source coerced to telemetry would be blended into the measured
        # side AND would slip past the RECONCILED_COLUMN check immediately below, which
        # is the guard that exists to catch exactly this row.
        raw_source = row.get("x_source")
        source = focus.SOURCE_TELEMETRY if raw_source is None else raw_source
        name = row["service_provider_name"]
        if source not in RECONCILED_COLUMN:
            # A row whose provenance this module does not recognise must not join either
            # side of a subtraction - but it must not VANISH either. The table carries no
            # CHECK constraint, so a hand-inserted or future-schema row can reach here,
            # and dropping it silently would quietly shrink the very report that exists
            # to announce when something has gone missing. Counted and named instead.
            unknown.setdefault(name, {})[source] = unknown.setdefault(name, {}).get(source, 0) + 1
            by_provider.setdefault(name, {})
            continue
        by_provider.setdefault(name, {}).setdefault(source, []).append(row)

    ever_seen = _sources_seen_by(billing_start)
    # A provider that used to report and reported NOTHING this period would otherwise
    # drop out of the report altogether, and its coverage would go dark in silence -
    # the exact disappearance this deliverable has to announce. It is carried with an
    # empty set of sides so that its coverage can be stated as zero and explained.
    # Only providers seen ON OR BEFORE this period qualify; one that first appears later
    # has not gone missing from this month, it had not arrived yet.
    for name in ever_seen:
        if provider is not None and name != provider:
            continue
        by_provider.setdefault(name, {})
    providers = []
    for name in sorted(by_provider):
        sides = {source: _side(source, rows_for)
                 for source, rows_for in by_provider[name].items()}
        telemetry = sides.get(focus.SOURCE_TELEMETRY)
        comparisons = []
        for source in sorted(sides):
            if source == focus.SOURCE_TELEMETRY:
                continue
            comparisons.append(_compare(telemetry, sides[source]))
        providers.append(
            {
                "service_provider_name": name,
                "coverage": _coverage(sides, ever_seen.get(name, set())),
                "sides": [sides[source] for source in sorted(sides)],
                "comparisons": comparisons,
                "excluded_rows": [
                    {"x_source": source, "rows": count,
                     "reason": "x_source_is_not_a_source_this_module_can_reconcile"}
                    for source, count in sorted(unknown.get(name, {}).items())
                ],
            }
        )

    return {
        "focus_version": focus.FOCUS_VERSION,
        "period": period,
        "billing_period_start": billing_start,
        "providers_with_excluded_rows": sorted(unknown),
        "degraded_providers": [
            entry["service_provider_name"] for entry in providers
            if entry["coverage"]["degraded"]
        ],
        "providers": providers,
    }


def _compare(telemetry: dict | None, other: dict) -> dict:
    """One source against telemetry, refusing to subtract totals that are not whole."""
    reason = None
    if telemetry is None:
        reason = "no_telemetry_rows_for_this_provider_period"
    elif telemetry["rows_without_cost"]:
        reason = (
            f"telemetry_side_incomplete:{telemetry['rows_without_cost']}"
            "_rows_carry_no_effective_cost"
        )
    elif other["rows_without_cost"]:
        reason = (
            f"{other['x_source']}_side_incomplete:{other['rows_without_cost']}"
            f"_rows_carry_no_{other['cost_column']}"
        )
    elif telemetry["total"] is None or other["total"] is None:
        reason = "a_side_has_no_costed_rows_at_all"

    comparison = {
        "x_source": other["x_source"],
        "compared_against": focus.SOURCE_TELEMETRY,
        "telemetry_total": None if telemetry is None else telemetry["total"],
        "source_total": other["total"],
        "comparable": reason is None,
        "difference": None,
        "difference_absent_reason": reason,
        "by_charge_category": [],
    }
    if reason is None:
        comparison["difference"] = other["total"] - telemetry["total"]

    other_by_category = {row["charge_category"]: row for row in other["by_charge_category"]}
    telemetry_by_category = (
        {} if telemetry is None
        else {row["charge_category"]: row for row in telemetry["by_charge_category"]}
    )
    for category in sorted(set(other_by_category) | set(telemetry_by_category)):
        billed = other_by_category.get(category)
        derived = telemetry_by_category.get(category)
        comparison["by_charge_category"].append(
            {
                "charge_category": category,
                "telemetry_total": None if derived is None else derived["total"],
                "telemetry_rows_without_cost": (
                    None if derived is None else derived["rows_without_cost"]
                ),
                "source_total": None if billed is None else billed["total"],
                "source_rows_without_cost": (
                    None if billed is None else billed["rows_without_cost"]
                ),
                # NEVER subtracted. See CATEGORY_DIFFERENCE_ABSENT_REASON: within a
                # category the two sides are not the same kind of quantity, so a
                # difference here would be a false discrepancy even on a month whose
                # overall difference is exactly 0.0.
                "difference": None,
                "difference_absent_reason": CATEGORY_DIFFERENCE_ABSENT_REASON,
            }
        )
    return comparison


def _sources_seen_by(billing_period_start: str) -> dict[str, set[str]]:
    """Which sources had produced a row for each provider ON OR BEFORE a billing period.

    DERIVED, never enumerated. A hand-written list of "sources we expect from claude"
    would be a second place to keep in step with reality, and it would go stale silently
    the first time a provider was added. The table's own history is the record.

    BOUNDED BY THE PERIOD BEING REPORTED ON, which an earlier draft was not. Unbounded,
    the history reached into the FUTURE: onboarding a vendor in August made every earlier
    month retroactively report it as a provider whose supply had collapsed to 0%, naming
    a source that had not existed yet. A historical report has to be stable - re-running
    July next year must give July's answer - and it cannot be if tomorrow can edit it.
    """
    focus.init()
    with closing(focus._connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT service_provider_name,
                      COALESCE(x_source, ?) AS source
               FROM focus_facts
               WHERE billing_period_start <= ?""",
            (focus.SOURCE_TELEMETRY, billing_period_start),
        ).fetchall()
    history: dict[str, set[str]] = {}
    for row in rows:
        # Only RECONCILABLE sources may enter the coverage denominator. A source this
        # module cannot reconcile is reported through `excluded_rows`, where it is
        # counted as PRESENT; admitting it here as well would let the same payload say a
        # row is present and that its source is absent, and would degrade coverage for a
        # row sitting right there in the table. It also stops an unknown source seen once
        # in an earlier period from becoming a permanently expected one.
        if row["source"] not in RECONCILED_COLUMN:
            continue
        history.setdefault(row["service_provider_name"], set()).add(row["source"])
    return history


def _coverage(sides: dict[str, dict], ever_seen: set[str]) -> dict:
    """How much of a provider-period we can actually see, and what is missing.

    TWO numbers, because they are aggregates of DIFFERENT things and one of them cannot
    answer the other's question. Each names its own denominator in the payload.

    `source_coverage_pct` = sources present this period / RECONCILABLE sources seen for
    this provider ON OR BEFORE this period. Its denominator is bounded HISTORY, and that
    is the whole point. A coverage figure
    taken over the rows that arrived would sit at 100% while a provider surface closed
    underneath it - the rows stop arriving, so both numerator and denominator shrink
    together and the number never moves. That silently-steady percentage is precisely the
    failure this deliverable exists to prevent, so the denominator is what we used to be
    able to see, not what we still can.

    `pricing_coverage_pct` = rows carrying a cost / rows present. Same NAME and same
    shape as everywhere else in this repo (`round(priced / total * 100, 1) if total else
    None`, with `unpriced_reasons` beside it), because it is the same concept and giving
    it a second name would have been the parallel vocabulary this deliverable was told
    not to invent. Its denominator is the rows that DID arrive, so it answers the other
    question: of what we can see, how much is priced. Not a substitute for the first and
    never blended with it.

    Its denominator is ROWS rather than quantity, unlike the token-weighted version in
    `cost_estimates`, and that is forced rather than chosen: a Purchase row consumes
    seat-months and a Usage row consumes tokens, so there is no quantity these rows share
    to weight by. `summary_for_period` already refuses to add those units together for
    the same reason, and inventing a common denominator here would have been worse than
    an unweighted count that names what it counts.

    Both are None when their denominator is zero, never 0.0: no rows at all is "we cannot
    say", not "nothing is covered".

    `degraded` is True when a source that used to report for this provider did not report
    this period, and `coverage_reasons` names each one. A number that shrinks without
    saying why is the thing being guarded against, so the reasons travel with it.
    """
    present = set(sides)
    expected = sorted(ever_seen | present)
    absent = [source for source in expected if source not in present]

    rows = sum(side["rows"] for side in sides.values())
    costed = sum(side["rows_with_cost"] for side in sides.values())

    unpriced = []
    for source in sorted(sides):
        for reason in sides[source]["uncosted_reasons"]:
            unpriced.append(reason)
    # One announcement list carrying BOTH kinds of gap, because a reader scanning for
    # "why is this number lower than I expected" must not have to know in advance which
    # of the two kinds they are looking for.
    reasons = [f"source_absent:{source}" for source in absent] + list(unpriced)

    return {
        "source_coverage_pct": (
            round(len(present) / len(expected) * 100, 1) if expected else None
        ),
        "sources_expected": expected,
        "sources_present": sorted(present),
        "sources_absent": absent,
        "source_coverage_denominator": "sources_seen_on_or_before_this_period",
        "pricing_coverage_pct": round(costed / rows * 100, 1) if rows else None,
        "rows": rows,
        "rows_with_cost": costed,
        "pricing_coverage_denominator": "rows_present_in_this_period",
        "unpriced_reasons": unpriced,
        "degraded": bool(absent),
        "coverage_reasons": reasons,
    }
