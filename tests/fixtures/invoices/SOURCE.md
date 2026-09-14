# Where this fixture's schema comes from

Access date: **2026-08-27** (America/Los_Angeles; the checks below ran at 23:48 local).
Recorded because a fixture whose provenance nobody wrote down becomes, within one campaign,
a thing people cite as though it were a vendor's real format.

## What could NOT be sourced, stated plainly

**No real vendor invoice export was available to this workspace, so this fixture is NOT a
copy of any vendor's file and does not imply fidelity to one.** The checks behind that
claim, so a reader can repeat them rather than take it on trust:

- `git ls-files` matching `invoice` or `billing`, case-insensitive: **no results**. There
  was no existing example in the repository to model.
- Outbound HTTP from this workspace is blocked: a request to `https://focus.finops.org`
  returned `HTTP Error 403: Forbidden` from the sandbox proxy, so no vendor's published
  invoice documentation could be retrieved and cited either.

## What the columns ARE grounded in, which is not nothing

The column names are not invented for this fixture. **Every column of the import contract
is a FOCUS 1.3 column this repository already emits**, taken from `FOCUS_CSV_COLUMNS` in
`src/focus.py` - the export mapping that shipped in item 2 phase 3 and was reviewed then:

| import column | FOCUS 1.3 name in `FOCUS_CSV_COLUMNS` |
|---|---|
| `service_provider_name` | `ServiceProviderName` |
| `service_name` | `ServiceName` |
| `charge_category` | `ChargeCategory` |
| `charge_period_start` | `ChargePeriodStart` |
| `charge_period_end` | `ChargePeriodEnd` |
| `billing_currency` | `BillingCurrency` |
| `billed_cost` | `BilledCost` |
| `effective_cost` | `EffectiveCost` |
| `pricing_quantity` | `PricingQuantity` |
| `pricing_unit` | `PricingUnit` |
| `consumed_quantity` | `ConsumedQuantity` |
| `consumed_unit` | `ConsumedUnit` |
| `billing_period_start` | `BillingPeriodStart` |
| `billing_period_end` | `BillingPeriodEnd` |
| `x_model` | `x_Model` |

So the contract is this repository's own export contract read backwards, and
`tests/test_invoice_ingest.py` asserts that relationship against `FOCUS_CSV_COLUMNS`
itself rather than against a copied list - a hand-written copy would go stale silently the
first time the export gained a column.

**It is our import contract, not a vendor's.** A vendor whose export uses different names
needs a per-vendor adapter that maps into this shape; writing one is out of scope for this
item, which delivers one worked example.

## What the three rows in `claude-2026-08.csv` are each there to prove

1. `Purchase` - a subscription line with both a billed and an effective cost stated.
2. `Tax` - a line the vendor states as `0.00`. A stated zero IS a measurement and must be
   stored as `0.0`, not turned into an absence.
3. `Usage` - a line the invoice shows with NO amount. An empty field is ABSENT and must be
   stored as `NULL`, not turned into a zero.

Rows 2 and 3 exist as a pair on purpose: they are the two halves of the rule this codebase
has already been corrected on twice, and a fixture that carried only one of them would let
a regression through in whichever direction it left untested.

## The contract itself, stated rather than left to be read off the parser

**Required, every one of them, on every row.** A file missing any of these is refused
whole, not ingested partially: `service_provider_name`, `charge_category`,
`charge_period_start`, `charge_period_end`, `billing_currency`, `billed_cost`.

**Optional.** `service_name` (defaults to the provider), `x_model`, `pricing_quantity`,
`pricing_unit`, `consumed_quantity`, `consumed_unit`, `effective_cost`,
`billing_period_start`, `billing_period_end`.

**A column outside those two lists refuses the whole file.** A header this parser does not
understand usually means the file is a different format, and ingesting the columns it
happened to recognise would produce a confidently partial month. A repeated column name
refuses the file too: `csv.DictReader` keeps the LAST value, so a file carrying
`billed_cost` twice would silently discard the first figure, and which one the vendor meant
is not ours to decide.

**Empty is ABSENT. `0` is a MEASUREMENT. A missing cell is neither.** An empty field
records NULL. A stated `0` records `0.0`. A row with fewer or more cells than the header is
a structural error and is rejected, because guessing which column a stray cell belonged to
would be inventing the input.

**Numbers must be finite.** `nan` and `inf` parse through `float()` and are refused anyway:
SQLite stores NaN as NULL, so an input stating something invalid would come back as the
absence this table reserves for "never measured", and an infinity poisons every total it
joins. Negative COSTS are accepted - a credit or refund is a real invoice line. Negative
QUANTITIES are refused; nothing consumes minus three tokens.

**Dates must be real days, and the end must be after the start.** A shape check accepts
`2026-02-31`, which no real-month query could ever match, so the cost would vanish from
every report while the row looked healthy.

**Period derivation, labelled honestly.** If `billing_period_start` and
`billing_period_end` are both supplied, they are used and the row records
`x_billing_period_basis = source_supplied_period`. If neither is supplied, the calendar
month around `charge_period_start` is derived and the row records
`derived_calendar_month_from_charge_period_start`. Supplying only one refuses the row.
A supplied billing period MUST start on the first of a month: every period query in this
repo looks a row up by a `YYYY-MM-01` key, so a row filed under a billing anniversary
would be unreachable by all of them. That is a real limitation of this contract and it is
stated rather than worked around.

**Source allowlist.** A file may be ingested only as `invoice` or `customer_export`.
`telemetry` is refused: it means this machine observed the row, so stamping an import with
it would fabricate provenance AND delete the provider-month's real telemetry, because
ingestion replaces the rows matching the source it writes.

**Replacement scope.** Ingesting replaces every row this SOURCE previously wrote for each
`(provider, billing period)` the file names - a corrected invoice supersedes rather than
duplicating. Rows from other sources are never touched. A provider-month the file does not
name is left alone; withdrawing a whole month means ingesting a file that still names it.

**One row per identity.** `(service_provider_name, charge_category, charge_period_start,
x_model)` must be unique within a file, matching the table's own uniqueness. Two lines
sharing it are reported as a named rejection against the offending line rather than
surfacing as a raw `sqlite3.IntegrityError` that names neither line nor file.

**Nothing is written if any line is rejected.** A partially ingested invoice looks like a
complete month, totals like an incomplete one, and records nowhere which lines are missing.
The caller gets every rejection at once instead.
