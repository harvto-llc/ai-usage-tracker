# The fleet receiver contract

What a receiver must do for the MINIMAL fleet agent's delivery accounting to be sound, and
what the agent guarantees in return.

This document exists because the alternative was worse. Before it, the agent's exactly-once
test ran against a deduplicating receiver that lived only in the test file, and a reviewer
was right to call that an invented convenience rather than a contract
(`CODEX` FAIL on `dd55015`, blocker 1). A test double is only evidence when it is the
conformance implementation of a written, reviewable specification. This is that
specification. `_Receiver(dedupe=True)` in `tests/test_fleet_push.py` implements it, and
`_Receiver(dedupe=False)` implements the pinned receiver that does NOT.

## Status of the two receivers

| Receiver | Conforms? | Consequence |
|---|---|---|
| `FULL src/otel_receiver.py` at `2f5ca9e7065135f260a5816328af0f78b6fa1292` | **No.** It accumulates additively (`otel_receiver.py:88-91`) and persists no record identifier. | A retry after a lost acknowledgement is counted twice. Asserted, not implied, by `test_the_receiver_at_2f5ca9e_double_counts_a_resend_because_it_does_not_dedupe`. |
| A conforming fleet receiver | Not built yet. It belongs to Enterprise AI-CUR by spec decision D5 (`ai-cur@4bfa2a3:specs/usage-tracker-tenancy/spec.md`). | Exactly-once as observed by the receiver, proven agent-side by `test_a_deduping_receiver_takes_the_resent_record_exactly_once`. |

The pinned receiver is a SHAPE reference. It tells the agent what payload will parse. It
does not, and was never built to, store `usage_tracker.*` values or dedupe anything.

## What the agent guarantees

1. **Every data point carries `usage_tracker.record.id`**, a 64-character lowercase hex
   digest derived from the record's own content. It is written to the buffer at enqueue and
   read back from the buffer on every send, so it is byte-identical across every attempt at
   the same record. (`test_a_lost_acknowledgement_resends_the_identical_record_id`.)
2. **Every data point carries `usage_tracker.install.id`**, so a row is attributable to one
   install. When the install has no identity the reserved marker `unknown` travels, never
   `''` and never an absent attribute.
3. **The agent never claims delivery without receiver evidence.** There is one column,
   `push_queue.receipt_json`; NULL is buffered, non-NULL holds what the receiver said. No
   sender-written flag exists, so "both" and "neither" are not representable.
4. **The agent does not duplicate a record against itself.** A batch is claimed inside a
   `BEGIN IMMEDIATE` transaction before it is sent, so two overlapping collector cycles
   cannot both send the same records. The claim is a lease, not a lock: it expires so a
   killed sender cannot strand real data forever.
5. **At-least-once is the delivery model, and the agent says so.** An acknowledgement can be
   lost after the receiver has already taken a batch, and no agent-side change can prevent
   the resend that follows without marking a record delivered on no evidence.

## What a conforming receiver MUST do

**R1. Persist the record id, and treat it as the idempotency key.** On accepting a data
point, store `usage_tracker.record.id` durably, scoped to `usage_tracker.install.id`. Two
installs may legitimately produce the same content digest; the pair is the key, not the
digest alone.

**R2. Take a repeated record id at most once.** A record id already stored must not be
counted, stored or applied a second time. This is the requirement the pinned receiver does
not meet.

**R3. Acknowledge an already-accepted record exactly as it acknowledges a fresh one.** This
is the clause that makes retry safe, and it is the one most likely to be got wrong. A retry
arrives precisely because the first acknowledgement was lost; if the receiver answers a
duplicate with an error or an omission, the agent keeps the record buffered forever and
retries it forever. `acceptedRecordIds` MUST therefore include every record id in the
request that the receiver holds, whether it was stored on this request or an earlier one.

**R4. Reply with the evidence the agent reads.** Preferred, and the only form that supports
partial acceptance:

```json
{"status": "ok", "acceptedRecordIds": ["<64 hex>", "..."]}
```

Accepted fallback, which the pinned receiver already emits:

```json
{"status": "ok", "dataPoints": 12}
```

The count form is weaker: it is evidence that this request's data points were traversed, and
it does not identify which records were stored. The agent accepts it only when the number
equals the number it sent, and it satisfies R4 only for a receiver that also meets R1-R3.

**R5. Make R1 and R2 atomic with respect to concurrent requests.** Two agents, or one agent
retrying while a first request is still in flight, must not both store the same record id.

**R6. Do not acknowledge before the record is durable.** An acknowledgement is a promise that
the record survives the receiver restarting. A receiver that acknowledges from memory and
then loses the record has told the agent to delete its only copy.

## What a conforming receiver MUST NOT do

- **Must not reject a duplicate.** See R3. A duplicate is the expected consequence of a lost
  acknowledgement, not a client error.
- **Must not add up `usage_tracker.*` values across exports.** They are gauges: each one is
  the total for that day as of that reading, not an increment. Adding them multiplies the
  figure by the number of collector cycles in the day. This is exactly the mistake the
  pinned receiver would make if these were emitted under its `claude_code.*` names, which is
  why they are not.
- **Must not require the token.** A receiver may authenticate, but the agent sends
  `Authorization: Bearer` only when a machine token is configured, and absent configuration
  is a supported state.

## The payload the agent sends

OTLP HTTP/JSON, derived from `FULL@2f5ca9e src/otel_receiver.py`. One `resourceMetrics`
entry, one `scopeMetrics` entry with `scope.name = com.amgad.usage-tracker.fleet`, and one
metric per reading name.

Each metric is a `gauge`, and the `gauge` object contains `dataPoints` and nothing else.
The OTLP metrics proto gives `Gauge` a single field and puts `aggregation_temporality` on
`Sum` and `Histogram` instead; an earlier version of the agent emitted it inside the gauge
and the pinned receiver accepted the invalid payload silently, because it reads `dataPoints`
and ignores unknown sibling keys. `test_the_gauge_carries_only_the_fields_otlp_gives_a_gauge`
now asserts the key set directly rather than trusting a lenient parser.

Integers travel as `asInt` carrying a JSON string, which is the OTLP encoding for int64 and
what `_extract_value` handles (`otel_receiver.py:227-241`). Floats travel as `asDouble`.
Attributes are the typed OTLP array (`otel_receiver.py:244-256`).

## Known residual, owned by the receiver

Against a receiver that does not meet R1-R3, a lost acknowledgement produces a duplicate
that the agent can neither prevent nor detect. The agent supplies the key that makes
deduplication possible; it cannot make a receiver use it. This is recorded as an open
receiver-side obligation rather than as a solved problem.
