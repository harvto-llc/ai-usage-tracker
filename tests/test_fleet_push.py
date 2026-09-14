"""The agent half of fleet mode: buffering, the wire contract, and delivery accounting.

THE RECEIVER DOUBLE IN HERE IS NOT THE CONTRACT. The contract is
`FULL src/otel_receiver.py` at commit `2f5ca9e7065135f260a5816328af0f78b6fa1292` in
`~/dev_projects/usage-tracker`, a different repository that this one must not import.
`_ReceiverParser` below is a deliberate transcription of the five lines of
`process_metrics_payload` that decide whether a payload is understood - the envelope
walk, the `sum or gauge or histogram` selection, `dataPoints`, `_extract_value`'s
`asInt`-then-`asDouble` order, and `_parse_attributes`' typed-value unwrapping - and each
transcribed line is cited where it is written. Validating an exporter against a double
alone proves self-consistency and nothing else, so the double's value here is only that
it is derived from a named, pinned source that a reviewer can diff it against.

The receivers are real HTTP servers on real sockets. "Unreachable" is a closed port and
"slow" is a handler that sleeps, because a mocked `urlopen` cannot fail the way a socket
fails and would let a timeout bug pass.
"""

import ast
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import src.database as db
from src import fleet_push, install_identity


# ── The receiver, transcribed from FULL@2f5ca9e src/otel_receiver.py ──────


class _ReceiverParser:
    """What `process_metrics_payload` does with a body, and nothing more."""

    def __init__(self):
        self.counts = {}
        self.attributes_seen = []
        self.values_seen = []

    def process(self, payload: dict) -> dict:
        # otel_receiver.py:75-79 — the envelope walk.
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    name = metric.get("name", "")
                    # otel_receiver.py:81 — sum, else gauge, else histogram.
                    data = (
                        metric.get("sum")
                        or metric.get("gauge")
                        or metric.get("histogram")
                        or {}
                    )
                    # otel_receiver.py:82
                    for dp in data.get("dataPoints", []):
                        self.values_seen.append(self._extract_value(dp))
                        self.attributes_seen.append(
                            self._parse_attributes(dp.get("attributes", []))
                        )
                        # otel_receiver.py:85
                        self.counts[name] = self.counts.get(name, 0) + 1
        return dict(self.counts)

    @staticmethod
    def _extract_value(data_point: dict) -> float:
        # otel_receiver.py:229-232 — asInt is read first and handed to int().
        if "asInt" in data_point:
            return int(data_point["asInt"])
        if "asDouble" in data_point:
            return float(data_point["asDouble"])
        return 0

    @staticmethod
    def _parse_attributes(attrs: list) -> dict:
        # otel_receiver.py:246-256
        result = {}
        for attr in attrs:
            key = attr.get("key", "")
            value = attr.get("value", {})
            for vtype in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if vtype in value:
                    result[key] = str(value[vtype])
                    break
        return result


class _Receiver:
    """A real HTTP receiver whose reply shape and behaviour a test chooses."""

    def __init__(self, *, status=200, reply="count", delay=0.0, dedupe=False):
        self.status = status
        self.reply = reply
        self.delay = delay
        self.dedupe = dedupe
        self.parser = _ReceiverParser()
        self.bodies = []
        self.record_ids_taken = []  # every record id the receiver has COUNTED, with repeats
        self.headers_seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the suite output clean
                pass

            def do_POST(self):
                if outer.delay:
                    time.sleep(outer.delay)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                outer.bodies.append(raw)
                outer.headers_seen.append(dict(self.headers))
                try:
                    payload = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"invalid JSON")
                    return

                accepted = outer._absorb(payload)
                body = outer._body(accepted)
                encoded = json.dumps(body).encode() if body is not None else b""
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/metrics"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _absorb(self, payload) -> int:
        """Count the payload the way the receiver does, honouring the dedupe setting."""
        counted = self._note_record_ids(payload, deduped=self.dedupe)
        if not self.dedupe:
            self.parser.process(payload)
        return counted

    def _note_record_ids(self, payload, *, deduped: bool) -> int:
        counted = 0
        already = set(self.record_ids_taken)
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    data = metric.get("sum") or metric.get("gauge") or {}
                    for dp in data.get("dataPoints", []):
                        attrs = _ReceiverParser._parse_attributes(dp.get("attributes", []))
                        rid = attrs.get(fleet_push.RECORD_ID_ATTRIBUTE)
                        if deduped and rid in already:
                            continue
                        self.record_ids_taken.append(rid)
                        counted += 1
        return counted

    def _body(self, accepted: int):
        if self.reply == "count":
            return {"status": "ok", "dataPoints": accepted}
        if self.reply == "wrong-count":
            return {"status": "ok", "dataPoints": accepted + 1}
        if self.reply == "record-ids":
            return {"status": "ok", "acceptedRecordIds": sorted(set(self.record_ids_taken))}
        if self.reply == "silent-ok":
            return {"status": "ok"}
        if self.reply == "not-json":
            return None
        raise AssertionError(f"unknown reply mode {self.reply!r}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    made = []

    def make(**kwargs):
        one = _Receiver(**kwargs)
        made.append(one)
        return one

    yield make
    for one in made:
        one.close()


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    """Every test gets its own buffer file, never the developer's."""
    monkeypatch.setenv(fleet_push.QUEUE_DB_ENV, str(tmp_path / "fleet_queue.db"))
    monkeypatch.delenv(fleet_push.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(fleet_push.TOKEN_ENV, raising=False)
    monkeypatch.delenv(fleet_push.ENABLED_ENV, raising=False)
    monkeypatch.delenv(fleet_push.TIMEOUT_ENV, raising=False)


READINGS = [
    ("usage_tracker.claude.tokens.today", 1234),
    ("usage_tracker.claude.messages.today", 7, {"type": "assistant"}),
]


def _closed_port_url() -> str:
    """A URL nothing is listening on. Bound then closed, so the port is really free."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/v1/metrics"


# ── Deliverable 1: buffer locally, first ─────────────────────────────────


def test_a_record_is_buffered_when_no_receiver_is_configured():
    assert fleet_push.endpoint() is None
    record_id = fleet_push.enqueue(READINGS)
    assert record_id is not None
    assert [row["record_id"] for row in fleet_push.pending()] == [record_id]


def test_a_buffered_record_outlives_the_process_that_wrote_it(tmp_path):
    """Durable means on disk, not in this interpreter's memory."""
    queue = tmp_path / "cross_process_queue.db"
    identity_file = tmp_path / "cross_process_install_id"
    env = {
        **os.environ,
        fleet_push.QUEUE_DB_ENV: str(queue),
        "USAGE_TRACKER_INSTALL_ID_FILE": str(identity_file),
    }
    env.pop(fleet_push.ENDPOINT_ENV, None)
    writer = subprocess.run(
        [sys.executable, "-c",
         "from src import fleet_push;"
         "print(fleet_push.enqueue([('usage_tracker.claude.tokens.today', 99)]))"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(), check=True,
    )
    written_id = writer.stdout.strip()
    assert len(written_id) == 64

    reader = subprocess.run(
        [sys.executable, "-c",
         "import json;from src import fleet_push;"
         "print(json.dumps([r['record_id'] for r in fleet_push.pending()]))"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(), check=True,
    )
    assert json.loads(reader.stdout) == [written_id]


def test_a_zero_valued_reading_is_buffered_rather_than_dropped():
    """`0` is a measurement. Only `None` is an absence.

    This is the rule this codebase has now relearned four times: `''`, `0` and `0.0` are
    all falsy and none of them mean absent.
    """
    record_id = fleet_push.enqueue([
        ("usage_tracker.claude.tokens.today", 0),
        ("usage_tracker.claude.cost.today", 0.0),
        ("usage_tracker.codex.messages.today", None),
    ])
    buffered = fleet_push.pending()[0]
    assert record_id == buffered["record_id"]
    names = [reading["name"] for reading in buffered["metrics"]]
    assert names == ["usage_tracker.claude.tokens.today", "usage_tracker.claude.cost.today"]
    assert [reading["value"] for reading in buffered["metrics"]] == [0, 0.0]


def test_a_reading_that_is_entirely_absent_buffers_nothing():
    assert fleet_push.enqueue([("usage_tracker.claude.tokens.today", None)]) is None
    assert fleet_push.pending() == []


def test_a_flag_is_refused_as_a_measurement():
    with pytest.raises(TypeError):
        fleet_push.enqueue([("usage_tracker.claude.tokens.today", True)])


# ── Deliverable 2: an OTLP-shaped exporter, stamped ──────────────────────


def test_the_export_is_understood_by_the_receivers_own_parser():
    fleet_push.enqueue(READINGS)
    batch = fleet_push.pending()
    export = fleet_push.build_export(batch, export_id="e1")

    parser = _ReceiverParser()
    counts = parser.process(export)

    assert counts == {
        "usage_tracker.claude.tokens.today": 1,
        "usage_tracker.claude.messages.today": 1,
    }
    # The values survive the receiver's own extraction, ints included, which is the
    # thing an `asInt` sent as a bare JSON number would still pass but a wrongly named
    # field would not.
    assert sorted(parser.values_seen) == [7, 1234]


def test_an_int_travels_as_a_string_under_asInt_and_a_float_under_asDouble():
    """The OTLP JSON encoding requires int64 as a string; `_extract_value` calls int()."""
    fleet_push.enqueue([
        ("usage_tracker.claude.tokens.today", 1234),
        ("usage_tracker.claude.cost.today", 1.25),
    ])
    export = fleet_push.build_export(fleet_push.pending(), export_id="e1")
    points = export["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    by_name = {metric["name"]: metric["gauge"]["dataPoints"][0] for metric in points}

    assert by_name["usage_tracker.claude.tokens.today"]["asInt"] == "1234"
    assert isinstance(by_name["usage_tracker.claude.tokens.today"]["asInt"], str)
    assert by_name["usage_tracker.claude.cost.today"]["asDouble"] == 1.25
    assert "asInt" not in by_name["usage_tracker.claude.cost.today"]


def test_every_data_point_carries_this_installs_stamp():
    expected = install_identity.stamp()
    assert install_identity.is_known(expected)

    fleet_push.enqueue(READINGS)
    export = fleet_push.build_export(fleet_push.pending(), export_id="e1")
    parser = _ReceiverParser()
    parser.process(export)

    assert parser.attributes_seen
    for attrs in parser.attributes_seen:
        assert attrs[fleet_push.INSTALL_ID_ATTRIBUTE] == expected


def test_an_install_with_no_identity_stamps_the_reserved_marker_not_an_empty_string(monkeypatch, tmp_path):
    """`unknown` is a claim about provenance. `''` would be a missing column."""
    unwritable = tmp_path / "no_such_dir" / "sub" / "install_id"
    monkeypatch.setenv("USAGE_TRACKER_INSTALL_ID_FILE", str(unwritable))
    install_identity.reset_cache()
    monkeypatch.setattr(install_identity, "_mint", lambda path: None)

    fleet_push.enqueue(READINGS)
    export = fleet_push.build_export(fleet_push.pending(), export_id="e1")
    parser = _ReceiverParser()
    parser.process(export)

    for attrs in parser.attributes_seen:
        assert attrs[fleet_push.INSTALL_ID_ATTRIBUTE] == install_identity.UNKNOWN_INSTALL_ID
        assert not install_identity.is_known(attrs[fleet_push.INSTALL_ID_ATTRIBUTE])


def test_absent_configuration_is_announced_as_a_state_and_pushes_nothing(receiver):
    listening = receiver()
    fleet_push.enqueue(READINGS)

    state = fleet_push.push_state()
    assert state["state"] == fleet_push.STATE_UNCONFIGURED
    assert state["reason"] == fleet_push.REASON_ENDPOINT_ABSENT

    result = fleet_push.flush()
    assert result["state"] == fleet_push.STATE_UNCONFIGURED
    assert result["acknowledged"] == 0
    # Nothing left this machine, and the record is still here.
    assert listening.bodies == []
    assert len(fleet_push.pending()) == 1


def test_an_empty_endpoint_string_is_absent_not_a_location(monkeypatch):
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, "   ")
    assert fleet_push.endpoint() is None
    assert fleet_push.push_state()["reason"] == fleet_push.REASON_ENDPOINT_ABSENT


def test_push_can_be_switched_off_explicitly_and_says_so(monkeypatch, receiver):
    listening = receiver()
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    monkeypatch.setenv(fleet_push.ENABLED_ENV, "off")
    fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["state"] == fleet_push.STATE_DISABLED
    assert result["reason"] == fleet_push.REASON_EXPLICITLY_DISABLED
    assert listening.bodies == []
    assert len(fleet_push.pending()) == 1


def test_the_machine_token_is_sent_when_configured_and_omitted_when_not(monkeypatch, receiver):
    listening = receiver()
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    fleet_push.enqueue(READINGS)
    fleet_push.flush()
    assert "Authorization" not in listening.headers_seen[0]

    monkeypatch.setenv(fleet_push.TOKEN_ENV, "machine-token-value")
    fleet_push.enqueue(READINGS, timestamp=int(time.time()) + 1)
    fleet_push.flush()
    assert listening.headers_seen[1]["Authorization"] == "Bearer machine-token-value"


# ── Deliverable 4: delivery is accounted for ─────────────────────────────


def test_a_record_is_delivered_only_against_the_count_the_receiver_reports(monkeypatch, receiver):
    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["state"] == fleet_push.STATE_PUSHED
    assert result["acknowledged"] == 1

    evidence = fleet_push.receipt(record_id)
    assert evidence["kind"] == fleet_push.ACK_BY_DATA_POINT_COUNT
    assert evidence["observed_data_points"] == 2
    assert evidence["sent_data_points"] == 2
    assert fleet_push.pending() == []


def test_a_count_that_disagrees_is_not_an_acknowledgement(monkeypatch, receiver):
    """The receiver said 200 and reported a different number. That is a discrepancy."""
    listening = receiver(reply="wrong-count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["state"] == fleet_push.STATE_READY
    assert result["reason"] == fleet_push.REASON_COUNT_MISMATCH
    assert result["status"] == 200
    assert fleet_push.receipt(record_id) is None
    assert [row["record_id"] for row in fleet_push.pending()] == [record_id]


def test_a_two_hundred_carrying_no_evidence_is_not_an_acknowledgement(monkeypatch, receiver):
    listening = receiver(reply="silent-ok")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["status"] == 200
    assert result["reason"] == fleet_push.REASON_NO_EVIDENCE
    assert result["acknowledged"] == 0
    assert fleet_push.receipt(record_id) is None
    assert len(fleet_push.pending()) == 1


def test_a_two_hundred_with_an_unreadable_body_is_not_an_acknowledgement(monkeypatch, receiver):
    listening = receiver(reply="not-json")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["reason"] == fleet_push.REASON_UNREADABLE_BODY
    assert fleet_push.receipt(record_id) is None


def test_a_receiver_that_names_the_records_it_took_acknowledges_exactly_those(monkeypatch, receiver):
    listening = receiver(reply="record-ids")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["reason"] == fleet_push.ACK_BY_RECORD_IDS
    evidence = fleet_push.receipt(record_id)
    assert record_id in evidence["observed_record_ids"]


def test_an_unreachable_receiver_leaves_the_record_buffered(monkeypatch):
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["reason"] == fleet_push.REASON_UNREACHABLE
    assert result["acknowledged"] == 0
    assert fleet_push.receipt(record_id) is None
    assert fleet_push.pending()[0]["attempts"] == 1


def test_a_receiver_returning_an_error_leaves_the_record_buffered(monkeypatch, receiver):
    listening = receiver(status=500)
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    result = fleet_push.flush()
    assert result["reason"] == fleet_push.REASON_HTTP_ERROR
    assert fleet_push.receipt(record_id) is None
    assert len(fleet_push.pending()) == 1


def test_a_slow_receiver_is_abandoned_at_the_configured_timeout(monkeypatch, receiver):
    listening = receiver(delay=2.0)
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    monkeypatch.setenv(fleet_push.TIMEOUT_ENV, "0.3")
    record_id = fleet_push.enqueue(READINGS)

    started = time.monotonic()
    result = fleet_push.flush()
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"flush blocked {elapsed:.2f}s past a 0.3s timeout"
    assert result["reason"] == fleet_push.REASON_UNREACHABLE
    assert fleet_push.receipt(record_id) is None


def test_an_acknowledged_record_is_never_sent_again(monkeypatch, receiver):
    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    fleet_push.enqueue(READINGS)

    assert fleet_push.flush()["acknowledged"] == 1
    second = fleet_push.flush()

    assert second["state"] == fleet_push.STATE_IDLE
    assert second["reason"] == fleet_push.REASON_NOTHING_PENDING
    assert len(listening.bodies) == 1

    # And still never, once the send claim has expired. Without this the assertions above
    # are satisfied by the claim lease rather than by the receipt, so "never" would only
    # have been proven for the length of one lease. Found by mutating the receipt filter
    # out of claim_batch and watching this test keep passing.
    far_future = int(time.time()) + int(fleet_push.claim_ttl_seconds()) + 1
    assert fleet_push.claim_batch(now=far_future) == []
    assert len(listening.bodies) == 1


def test_a_record_is_buffered_or_delivered_and_never_both_and_never_neither(monkeypatch, receiver):
    """The invariant, checked across a failure and a success on the same record."""
    unreachable = _closed_port_url()
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, unreachable)
    record_id = fleet_push.enqueue(READINGS)

    fleet_push.flush()
    assert fleet_push.counts() == {
        "total": 1, "buffered": 1, "delivered": 0,
        "delivered_total": 0, "discarded_total": 0,
    }
    assert fleet_push.receipt(record_id) is None

    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    fleet_push.flush()
    assert fleet_push.counts() == {
        "total": 1, "buffered": 0, "delivered": 1,
        "delivered_total": 1, "discarded_total": 0,
    }
    assert fleet_push.receipt(record_id) is not None


def test_a_lost_acknowledgement_resends_the_identical_record_id(monkeypatch, receiver):
    """At-least-once, stated rather than wished away.

    The receiver counts the batch and then the reply is lost. The record is still
    buffered, so the next flush resends it - carrying the SAME `usage_tracker.record.id`,
    which is the only thing that lets a receiver recognise it.
    """
    listening = receiver(reply="silent-ok")  # counted, but no evidence comes back
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    fleet_push.flush()
    assert fleet_push.receipt(record_id) is None

    listening.reply = "count"
    fleet_push.flush()

    assert len(listening.bodies) == 2
    first_ids = set(_ReceiverParser._parse_attributes(dp.get("attributes", []))[fleet_push.RECORD_ID_ATTRIBUTE]
                    for dp in _points(listening.bodies[0]))
    second_ids = set(_ReceiverParser._parse_attributes(dp.get("attributes", []))[fleet_push.RECORD_ID_ATTRIBUTE]
                     for dp in _points(listening.bodies[1]))
    assert first_ids == second_ids == {record_id}


def test_the_receiver_at_2f5ca9e_double_counts_a_resend_because_it_does_not_dedupe(monkeypatch, receiver):
    """The limitation, measured rather than implied away.

    `process_metrics_payload` accumulates (`otel_receiver.py:88-91`); it has no
    idempotency key and no dedupe. So the resend proven above IS counted twice by that
    receiver. The agent supplies the key that makes deduping possible; it cannot make a
    receiver use it. Asserted here so nobody reads the previous test as a claim that
    duplicates are impossible.
    """
    listening = receiver(reply="silent-ok", dedupe=False)
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    fleet_push.flush()
    fleet_push.flush()

    assert listening.record_ids_taken == [record_id] * 4  # 2 data points, twice


def test_a_deduping_receiver_takes_the_resent_record_exactly_once(monkeypatch, receiver):
    """The same resend against a receiver that uses the key the agent supplies."""
    listening = receiver(reply="silent-ok", dedupe=True)
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)

    fleet_push.flush()
    fleet_push.flush()

    assert len(listening.bodies) == 2
    assert listening.record_ids_taken == [record_id, record_id]  # 2 data points, once


def test_the_receipt_is_the_receivers_number_and_not_the_senders(monkeypatch, receiver):
    """A sender-written flag would survive a receiver that reported a different number."""
    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    record_id = fleet_push.enqueue(READINGS)
    fleet_push.flush()

    evidence = fleet_push.receipt(record_id)
    # The number in the receipt is the number the receiver's own parser arrived at, not
    # a figure the sender computed about itself.
    assert evidence["observed_data_points"] == sum(listening.parser.counts.values())
    assert evidence["endpoint"] == listening.url


# ── The OTLP schema itself, not the lenient parser's opinion of it ───────


def test_the_gauge_carries_only_the_fields_otlp_gives_a_gauge():
    """The OTLP metrics proto's Gauge message has exactly one field, `data_points`.

    `aggregation_temporality` belongs to Sum and Histogram. An earlier version of this
    module put it inside the gauge and `_ReceiverParser` accepted it happily, because the
    receiver reads `dataPoints` and ignores unknown sibling keys - which is why this
    asserts the key SET rather than asking a lenient parser whether it minded.
    """
    fleet_push.enqueue(READINGS)
    export = fleet_push.build_export(fleet_push.pending(), export_id="e1")
    metrics = export["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]

    assert metrics
    for metric in metrics:
        assert set(metric) == {"name", "gauge"}
        assert set(metric["gauge"]) == fleet_push.GAUGE_FIELDS == {"dataPoints"}
        for point in metric["gauge"]["dataPoints"]:
            unexpected = set(point) - fleet_push.NUMBER_DATA_POINT_FIELDS
            assert not unexpected, f"not a NumberDataPoint field: {sorted(unexpected)}"
            # asInt and asDouble are the two arms of one `oneof value`.
            assert len({"asInt", "asDouble"} & set(point)) == 1


# ── Duplicates the agent would create by itself ──────────────────────────


def test_two_senders_cannot_both_claim_the_same_record():
    """The claim is taken inside the transaction that reads the rows, so it is atomic."""
    record_id = fleet_push.enqueue(READINGS)

    first = fleet_push.claim_batch()
    second = fleet_push.claim_batch()

    assert [row["record_id"] for row in first] == [record_id]
    assert second == [], "a second sender claimed a record the first was already sending"


def test_a_claimed_record_is_still_reported_as_buffered():
    """Claiming decides who SENDS. Only a receipt decides delivery."""
    record_id = fleet_push.enqueue(READINGS)
    fleet_push.claim_batch()

    assert [row["record_id"] for row in fleet_push.pending()] == [record_id]
    assert fleet_push.counts() == {
        "total": 1, "buffered": 1, "delivered": 0,
        "delivered_total": 0, "discarded_total": 0,
    }
    assert fleet_push.receipt(record_id) is None


def test_a_failed_send_gives_the_claim_back_for_the_next_cycle(monkeypatch, receiver):
    """A claim held after a failure would delay the retry by the whole lease."""
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    fleet_push.enqueue(READINGS)

    assert fleet_push.flush()["reason"] == fleet_push.REASON_UNREACHABLE

    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    assert fleet_push.flush()["acknowledged"] == 1, "the retry did not go out immediately"


def test_a_claim_abandoned_by_a_dead_sender_expires(monkeypatch):
    """A lease, not a lock. A killed sender must not strand real data forever."""
    fleet_push.enqueue(READINGS)
    fleet_push.claim_batch(now=1_000_000)

    assert fleet_push.claim_batch(now=1_000_000) == []
    later = 1_000_000 + int(fleet_push.claim_ttl_seconds()) + 1
    assert len(fleet_push.claim_batch(now=later)) == 1


def test_a_batch_held_by_another_sender_is_announced_as_such(monkeypatch, receiver):
    """"Someone else has it" and "there is nothing to send" are different facts."""
    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    fleet_push.enqueue(READINGS)
    fleet_push.claim_batch()

    result = fleet_push.flush()

    assert result["state"] == fleet_push.STATE_IDLE
    assert result["reason"] == fleet_push.REASON_CLAIMED_ELSEWHERE
    assert listening.bodies == []


def test_real_concurrent_senders_deliver_the_record_once(tmp_path, receiver):
    """Two collector cycles overlapping, in real processes, against one receiver.

    This is the duplicate class the agent owns. It is nothing to do with the receiver's
    idempotency: two senders reading the same buffered rows would send them twice even
    against a perfect receiver.
    """
    listening = receiver(reply="count")
    queue = tmp_path / "concurrent_queue.db"
    env = {
        **os.environ,
        fleet_push.QUEUE_DB_ENV: str(queue),
        fleet_push.ENDPOINT_ENV: listening.url,
        "USAGE_TRACKER_INSTALL_ID_FILE": str(tmp_path / "concurrent_install_id"),
    }
    seed = subprocess.run(
        [sys.executable, "-c",
         "from src import fleet_push;"
         "print(fleet_push.enqueue([('usage_tracker.claude.tokens.today', 42)]))"],
        capture_output=True, text=True, env=env, cwd=os.getcwd(), check=True,
    )
    record_id = seed.stdout.strip()
    assert len(record_id) == 64

    senders = [
        subprocess.Popen(
            [sys.executable, "-c",
             "import json;from src import fleet_push;"
             "print(json.dumps(fleet_push.flush()['acknowledged']))"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=os.getcwd(),
        )
        for _ in range(4)
    ]
    acknowledged = []
    for sender in senders:
        out, err = sender.communicate(timeout=60)
        assert sender.returncode == 0, err
        acknowledged.append(json.loads(out.strip()))

    assert sum(acknowledged) == 1, f"more than one sender claimed delivery: {acknowledged}"
    assert listening.record_ids_taken == [record_id], (
        "the receiver saw the record more than once: "
        f"{listening.record_ids_taken}"
    )


def _points(raw: bytes) -> list:
    payload = json.loads(raw)
    out = []
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                data = metric.get("sum") or metric.get("gauge") or {}
                out.extend(data.get("dataPoints", []))
    return out


def test_the_queue_never_resolves_into_the_developers_home_under_test():
    """The path this test's own process would open is outside the developer's home."""
    resolved = fleet_push.queue_path().resolve()
    home = Path.home().resolve()
    assert home not in resolved.parents, f"the suite would write {resolved} into {home}"


def test_every_test_in_the_repo_gets_the_queue_isolated_not_just_this_file():
    """Derived from conftest's syntax tree, because the runtime check cannot see this.

    The assertion above passes whether or not the repo-level fixture exists, because THIS
    file also isolates itself - so it is blind to exactly the failure that occurred. A run
    of the full suite created `~/.usage-tracker/fleet_queue.db` on the developer's machine
    while this file alone created nothing: the pollution came from `src/collector.py:main`
    being exercised in `tests/test_collector.py`, which had no reason to know fleet push
    existed. Proven by mutation: deleting the conftest fixture leaves the runtime check
    green, and fails this one.

    A grep would match the docstring that explains the fixture, so it would pass on a file
    that merely still talks about isolation. The AST sees the decorator and the call.
    """
    tree = ast.parse(pathlib.Path("tests/conftest.py").read_text(encoding="utf-8"))

    isolating = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        autouse = any(
            isinstance(decorator, ast.Call)
            and any(
                keyword.arg == "autouse"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in decorator.keywords
            )
            for decorator in node.decorator_list
        )
        if not autouse:
            continue
        sets_queue_env = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "setenv"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == fleet_push.QUEUE_DB_ENV
            for call in ast.walk(node)
        )
        if sets_queue_env:
            isolating.append(node.name)

    assert isolating, (
        f"no autouse fixture in tests/conftest.py redirects {fleet_push.QUEUE_DB_ENV}, so a "
        "test that exercises the collector writes a queue into the developer's home"
    )


# ── The queue is bounded, and every loss is announced ────────────────────


def test_an_install_with_no_endpoint_buffers_nothing():
    """Not a fleet member. Nothing to deliver, so nothing to keep.

    Measured before this rule existed: 1440 cycles with no endpoint - one day at the
    collector's 60s cadence - retained 1440 rows, 100% never acknowledged, 1,142,784
    bytes, projecting to 0.388 GiB per install per year of records that by construction
    could never be delivered. Every Lite install is in this state, because no receiver
    exists yet.
    """
    assert fleet_push.endpoint() is None

    outcome = fleet_push.record_cycle(READINGS)

    assert outcome["state"] == fleet_push.STATE_UNCONFIGURED
    assert outcome["reason"] == fleet_push.REASON_NOT_A_FLEET_MEMBER
    assert outcome["buffered"] is False
    assert outcome["record_id"] is None
    assert fleet_push.counts()["total"] == 0


def test_a_configured_but_unreachable_install_still_buffers(monkeypatch):
    """The distinction the rule turns on. Unreachable is what buffering is FOR."""
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())

    outcome = fleet_push.record_cycle(READINGS)

    assert outcome["buffered"] is True
    assert outcome["reason"] == fleet_push.REASON_UNREACHABLE
    assert fleet_push.counts()["buffered"] == 1


def test_a_configured_install_with_push_paused_still_buffers(monkeypatch, receiver):
    """Membership is decided by the endpoint, not by the pause switch."""
    listening = receiver()
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    monkeypatch.setenv(fleet_push.ENABLED_ENV, "off")

    outcome = fleet_push.record_cycle(READINGS)

    assert outcome["state"] == fleet_push.STATE_DISABLED
    assert outcome["buffered"] is True
    assert listening.bodies == []
    assert fleet_push.counts()["buffered"] == 1


def test_the_backlog_stops_growing_once_it_hits_the_cap(monkeypatch):
    """Driven past the cap, not argued about."""
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    monkeypatch.setenv(fleet_push.MAX_BUFFERED_ENV, "50")
    base = 1_700_000_000

    for cycle in range(200):
        fleet_push.enqueue([("usage_tracker.claude.tokens.today", cycle)],
                           timestamp=base + cycle * 60)

    tally = fleet_push.counts()
    assert tally["buffered"] == 50, "the backlog kept growing past its cap"
    assert tally["discarded_total"] == 150

    # The OLDEST went. For a daily-total gauge the newest reading supersedes the older
    # ones, so the stale end is the cheap end to lose.
    kept = [row["created_at"] for row in fleet_push.pending(limit=100)]
    assert kept == [base + cycle * 60 for cycle in range(150, 200)]


def test_a_discarded_record_is_announced_and_never_dropped_in_silence(monkeypatch):
    """A queue that sheds its oldest rows quietly is the number that quietly shrinks."""
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    monkeypatch.setenv(fleet_push.MAX_BUFFERED_ENV, "3")
    base = 1_700_000_000

    outcomes = [
        fleet_push.record_cycle([("usage_tracker.claude.tokens.today", cycle)],
                                timestamp=base + cycle * 60)
        for cycle in range(5)
    ]

    assert [one["discarded"] for one in outcomes] == [0, 0, 0, 1, 1]
    assert [one["reason"] for one in outcomes[3:]] == [fleet_push.REASON_BACKLOG_CAPPED] * 2
    assert fleet_push.counts()["discarded_total"] == 2


def test_pruning_delivered_rows_never_makes_the_delivered_count_fall(monkeypatch, receiver):
    """The rows are bounded evidence; the lifetime total is history and does not shrink."""
    listening = receiver(reply="count")
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, listening.url)
    monkeypatch.setenv(fleet_push.DELIVERED_RETENTION_ENV, "2")
    base = 1_700_000_000

    for cycle in range(5):
        fleet_push.enqueue([("usage_tracker.claude.tokens.today", cycle)],
                           timestamp=base + cycle * 60)
        assert fleet_push.flush()["acknowledged"] == 1

    tally = fleet_push.counts()
    assert tally["delivered"] == 2, "delivered rows were not pruned to the retention window"
    assert tally["delivered_total"] == 5, "the lifetime delivered count shrank when rows were pruned"
    assert tally["buffered"] == 0


def test_the_default_cap_bounds_the_queue_to_a_size_measured_from_real_rows(monkeypatch, tmp_path):
    """The cap is derived from the cadence and checked against a MEASURED row size.

    Not a hand-picked constant blessed by a hand-picked assertion: the bytes per record
    come from a real populated queue file in this test, and the bound is computed from
    them. If a schema change makes rows much fatter, this fails.
    """
    assert fleet_push.DEFAULT_MAX_BUFFERED_RECORDS == 24 * 60 * 7 == 10080

    queue = tmp_path / "sized.db"
    monkeypatch.setenv(fleet_push.QUEUE_DB_ENV, str(queue))
    sample = 400
    base = 1_700_000_000
    for cycle in range(sample):
        fleet_push.enqueue(
            [
                ("usage_tracker.claude.messages.today", cycle),
                ("usage_tracker.claude.tokens.today", cycle * 100),
                ("usage_tracker.claude.projects.today", 3),
                ("usage_tracker.codex.messages.today", cycle),
                ("usage_tracker.codex.tokens.today", cycle * 7, {"type": "input"}),
                ("usage_tracker.codex.tokens.today", cycle * 9, {"type": "output"}),
            ],
            timestamp=base + cycle * 60,
        )

    on_disk = sum(path.stat().st_size for path in tmp_path.glob("sized.db*"))
    bytes_per_record = on_disk / sample
    worst_case = (
        fleet_push.DEFAULT_MAX_BUFFERED_RECORDS + fleet_push.DEFAULT_DELIVERED_RETENTION
    ) * bytes_per_record

    assert bytes_per_record < 4096, f"a record grew to {bytes_per_record:.0f} bytes"
    assert worst_case < 64 * 1024 * 1024, (
        f"the bounded queue would reach {worst_case / 1024 / 1024:.1f} MiB"
    )


# ── Deliverable 3: local-first operation is preserved ────────────────────


def _local_surfaces_still_work(tmp_path, monkeypatch, name: str) -> None:
    """Write through the local database and read it back, ignoring the fleet half."""
    path = tmp_path / f"{name}.db"
    monkeypatch.setattr(db, "DB", path)
    db.init()
    db.insert(session=12.5, weekly=30.0, ts=1_700_000_000)

    latest = db.latest_sample()
    assert latest is not None, f"local dashboard read returned nothing, receiver {name!r}"
    assert latest["session"] == 12.5
    assert latest["weekly"] == 30.0
    assert db.last_samples(5) == [(1_700_000_000, 12.5, 30.0, 0.0)]

    stamped = sqlite3.connect(path).execute(
        "SELECT install_id FROM usage_samples ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()[0]
    assert stamped == install_identity.stamp()


@pytest.mark.parametrize("condition", ["absent", "unreachable", "erroring", "slow"])
def test_the_local_dashboard_is_unaffected_by_the_receivers_condition(
    condition, tmp_path, monkeypatch, receiver
):
    """Local-first, in all four states the receiver can be in.

    `docs/deployment-privacy.md` in the business repo commits to the developer's own
    dashboard continuing to work offline, and today "offline" is the default because no
    receiver exists yet.
    """
    if condition == "absent":
        pass
    elif condition == "unreachable":
        monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    elif condition == "erroring":
        monkeypatch.setenv(fleet_push.ENDPOINT_ENV, receiver(status=500).url)
    else:
        monkeypatch.setenv(fleet_push.ENDPOINT_ENV, receiver(delay=2.0).url)
        monkeypatch.setenv(fleet_push.TIMEOUT_ENV, "0.3")

    outcome = fleet_push.record_cycle(READINGS)
    assert outcome["acknowledged"] == 0
    assert outcome["reason"], "a skip must name itself"
    _local_surfaces_still_work(tmp_path, monkeypatch, condition)


def test_the_collector_cycle_survives_a_fleet_half_that_raises(monkeypatch):
    """The fleet step is additive: a broken one must not stop local collection."""
    import src.collector as collector

    def explode(*args, **kwargs):
        raise RuntimeError("fleet half is broken")

    # An endpoint MUST be configured or the fleet step returns at the membership guard and
    # never reaches these stubs, which would make this test pass without exercising a
    # single line of the fleet half. Found by mutation: narrowing `except Exception` to
    # `except ZeroDivisionError` left this test green until the endpoint was set.
    monkeypatch.setenv(fleet_push.ENDPOINT_ENV, _closed_port_url())
    monkeypatch.setattr(collector.fleet_push, "enqueue", explode)
    monkeypatch.setattr(collector.fleet_push, "flush", explode)

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

    posted = []

    class _Response:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        posted.append(request.full_url)
        return _Response()

    monkeypatch.setattr(collector.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(collector, "sync", lambda: None)
    monkeypatch.setenv("USAGE_TRACKER_ACCESS", "api")

    collector.main()

    assert posted == [collector.API_URL], "the local report did not go out"


def test_the_collector_reports_the_fleet_state_rather_than_staying_silent(monkeypatch, capsys):
    """A skip is a fail-open unless it announces itself."""
    import src.collector as collector

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

    collector.main()

    printed = capsys.readouterr().out
    assert "Fleet push: unconfigured (not_a_fleet_member)" in printed
    # And it buffered NOTHING, because an install with no endpoint is not a fleet member.
    # Before the supervisor's ruling this asserted `== 1`, which is what made every Lite
    # install accrete 0.39 GiB a year of records that could never be delivered.
    assert fleet_push.counts()["buffered"] == 0
