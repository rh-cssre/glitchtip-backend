"""Golden lock on the envelope-ingest contract.

``POST /api/<project_id>/envelope/`` is served by the Rust pipeline via
``glitchtip/rust_ingest.py`` (minidump envelopes are handed back to the
Django fallback view pre-side-effect). This module locks the observable
contract of that endpoint: every test posts raw request bytes through
``IngestDispatcher`` (the seam production traffic enters) and asserts the
outcome — response status/body/headers-subset, the canonicalized enqueued
vtasks message(s), and the DB rows after draining those messages through the
real task functions — against a committed golden
(``parity_fixtures/goldens.json``).

The goldens were recorded as a parity lock while the endpoint had two arms
(the Python view was the default until the Rust pipeline replaced it), so
they double as the contract the retired Python pipeline honored — a golden
mismatch means the endpoint's observable behavior changed, and the change
should be deliberate. The one behavior recorded here that the Python view
never had: ``header_dsn_only``, where the credential appears only in the
envelope-header ``dsn`` field and the Rust pipeline recovers it (the view
reads only the query string / auth headers and denies).

One canonicalization rule matters for reading the goldens: enqueued
event/transaction/user-report messages are compared AFTER re-validation
through the worker-side schema (see ``_canon_message``), because the wire
payload's serialization is an implementation detail while the worker
schema — the only consumer — normalizes it.

Regenerating goldens: run with ``GLITCHTIP_INGEST_PARITY_RECORD=<path>`` set
to write the observed results to ``<path>`` instead of asserting (each test
is reported as skipped), diff against ``goldens.json``, and update it by
hand so behavior changes stay explicit.
"""

import gzip
import importlib
import json
import os
import time
import unittest
from pathlib import Path

try:
    # Stdlib zstd (PEP 784) landed in Python 3.14; only the *compressor* is
    # needed here (runtime decompression happens in gt_rust), so the 3.12 CI
    # lane simply skips the zstd case.
    from compression import zstd
except ImportError:
    zstd = None

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.tasks import task_backends
from django.utils import timezone

from apps.issue_events.models import IssueEvent
from apps.logs.models import LogEvent
from apps.performance.models import TransactionGroup

from .utils import AsgiIngestTestMixin, EventIngestTestCase, list_to_envelope

FIXTURES = Path(__file__).parent / "parity_fixtures"
GOLDENS_PATH = FIXTURES / "goldens.json"
RECORD_PATH = os.environ.get("GLITCHTIP_INGEST_PARITY_RECORD")

# The response headers the contract locks. Everything else (Date, Server,
# middleware security headers, ...) is deployment noise, not ingest behavior.
SIGNIFICANT_HEADERS = ("content-type", "retry-after", "access-control-allow-origin")

# Keys masked in every canonicalized structure: generated per request, never
# comparable across runs.
ALWAYS_MASKED = frozenset({"received", "uuid"})

_goldens_cache = None


def _goldens() -> dict:
    global _goldens_cache
    if _goldens_cache is None:
        _goldens_cache = json.loads(GOLDENS_PATH.read_text())
    return _goldens_cache


@unittest.skipIf(
    not settings.VALKEY_URL,
    "parity goldens assume within-batch FIFO enqueue order, which the "
    "Postgres broker's same-timestamp batch INSERT does not define; the "
    "wire bytes are broker-independent (identical serialize() blobs), and "
    "the Postgres-only lane is covered end-to-end by RustIngestTestCase",
)
class IngestParityTestCase(AsgiIngestTestMixin, EventIngestTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        cache.clear()
        # Rust writes its block/dedupe/queue state to the dedicated test
        # Valkey database — start each test from a clean slate.
        from glitchtip.rust_ingest import _valkey_driver

        _valkey_driver().flushdb_sync()

    # -- canonicalization ---------------------------------------------------

    def _canon(self, obj, masks: frozenset = frozenset()):
        """Canonicalize a JSON-compatible structure for cross-run
        comparison: mask non-deterministic fields and substitute
        the per-run project/organization ids (asserting they are the right
        ones before masking, so a message for the wrong project can't hide
        behind the placeholder)."""
        if isinstance(obj, dict):
            out = {}
            for key in sorted(obj):
                value = obj[key]
                if key == "project_id" and isinstance(value, int):
                    self.assertEqual(value, self.project.id)
                    out[key] = "<project_id>"
                elif key == "organization_id" and isinstance(value, int):
                    self.assertEqual(value, self.organization.id)
                    out[key] = "<organization_id>"
                elif key in ALWAYS_MASKED or key in masks:
                    out[key] = None if value is None else f"<{key}>"
                else:
                    out[key] = self._canon(value, masks)
            return out
        if isinstance(obj, list):
            return [self._canon(item, masks) for item in obj]
        return obj

    def _canon_response(self, status: int, headers: dict, body: bytes) -> dict:
        lowered = {name.lower(): value for name, value in headers.items()}
        try:
            body_out = json.loads(body)
        except ValueError:
            body_out = {"<text>": body.decode("utf-8", errors="replace")}
        return {
            "status": status,
            "headers": {
                name: lowered[name] for name in SIGNIFICANT_HEADERS if name in lowered
            },
            "body": body_out,
        }

    # -- queue draining -----------------------------------------------------

    async def _drain(self, masks: frozenset = frozenset()) -> list:
        """Pop everything the endpoint enqueued, canonicalize it, then run
        it through the REAL task functions so the DB assertions also prove
        the messages are consumable exactly as a worker would consume them.

        The Rust pipeline enqueues to the test Valkey queue; anything it
        hands back to the Python fallback view (the minidump fallback) lands
        in the immediate test backend instead — so both sources are drained
        unconditionally, in that order.
        """
        raw_messages = []
        from django_vtasks.serialization import deserialize

        from glitchtip.rust_ingest import _valkey_driver

        driver = _valkey_driver()
        while True:
            raw = await driver.rpop("{vt}:q:ingest")
            if raw is None:
                break
            raw_messages.append(deserialize(bytes(raw)))
        for message in raw_messages:
            module, name = message["func"].rsplit(".", 1)
            vtask = getattr(importlib.import_module(module), name)
            # Ingest tasks are batched consumers: each call takes a list
            # of task-message dicts.
            await vtask.func([message])
        backend = task_backends["default"]
        raw_messages += list(backend.pending_batches.get("ingest", []))
        # The immediate backend's flush runs async task functions through
        # async_to_sync, so it needs a thread.
        await sync_to_async(backend.flush_batches)()
        return [self._canon_message(message, masks) for message in raw_messages]

    def _canon_message(self, message: dict, masks: frozenset) -> dict:
        """Reduce a wire message to the canonical ``{"func", "args",
        "kwargs"}`` shape (the Rust pipeline's extra wire fields — the
        generated vtasks envelope ``id`` — are dropped).

        Event/transaction/user-report payloads are re-validated through the
        worker-side schema (the exact ``_validate_batch`` step the worker
        applies) and compared as that schema's canonical JSON dump. The two
        enqueue sources serialize the wire payload differently — the Python
        fallback view enqueues its full pydantic dump (explicit nulls,
        listed header pairs), the Rust pipeline forwards the compact
        validated SDK JSON — and the worker schema is the deliberate
        semantic equalizer (see the ``_validate_batch`` docstring in
        tasks.py). A payload the worker would parse differently WILL
        diverge here."""
        from apps.event_ingest.schema import (
            InterchangeTransactionEvent,
            IssueTaskMessage,
            UserReportTaskMessage,
        )

        worker_schemas = {
            "apps.event_ingest.tasks.ingest_event": IssueTaskMessage,
            "apps.event_ingest.tasks.ingest_transaction": InterchangeTransactionEvent,
            "apps.event_ingest.tasks.ingest_user_report": UserReportTaskMessage,
            # apps.logs.tasks.ingest_logs consumes the raw dicts (both
            # enqueue sources already send the flattened normalized items).
        }
        args = list(message.get("args") or [])
        schema = worker_schemas.get(message["func"])
        if schema is not None and len(args) == 1:
            args = [schema(**args[0]).model_dump(mode="json")]
        return {
            "func": message["func"],
            "args": self._canon(args, masks),
            "kwargs": self._canon(message.get("kwargs") or {}, masks),
        }

    async def _db_state(self) -> dict:
        events = [
            {
                "event_id": event.event_id.hex,
                "title": event.title,
                "level": int(event.level),
                "ip_address": (event.data.get("user") or {}).get("ip_address"),
            }
            async for event in IssueEvent.objects.order_by("event_id")
        ]
        transactions = [
            {"transaction": group.transaction, "op": group.op}
            async for group in TransactionGroup.objects.order_by("transaction")
        ]
        logs = [
            {"body": log.body, "service": log.service}
            async for log in LogEvent.objects.order_by("body")
        ]
        return {
            "issue_events": events,
            "issue_count": await self.project.issues.acount(),
            "transaction_groups": transactions,
            "logs": logs,
        }

    # -- golden checking ----------------------------------------------------

    async def _run_case(
        self,
        name: str,
        body: bytes,
        masks: frozenset = frozenset(),
        **post_kwargs,
    ) -> dict:
        status, headers, response_body = await self._post(body, **post_kwargs)
        observed = {
            "response": self._canon_response(status, headers, response_body),
            "messages": await self._drain(masks),
            "db": self._canon(await self._db_state(), masks),
        }
        self._check(name, observed)
        return observed

    def _check(self, name: str, observed: dict) -> None:
        if RECORD_PATH:
            path = Path(RECORD_PATH)
            data = json.loads(path.read_text()) if path.exists() else {}
            data[name] = observed
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            self.skipTest(f"recorded observed golden for {name!r}")
        self.assertEqual(
            observed,
            _goldens()[name],
            f"envelope ingest diverged from the committed golden for {name!r}",
        )

    # -- fixtures -----------------------------------------------------------

    def _fixture(self, filename: str) -> bytes:
        return (FIXTURES / filename).read_bytes()

    # -- cases: happy paths ---------------------------------------------------

    async def test_simple_event(self):
        """Plain JSON error-event envelope, credential in the query string."""
        await self._run_case(
            "simple_event",
            self._fixture("simple_event.envelope"),
            extra_headers=[(b"origin", b"https://app.example")],
        )

    async def test_simple_event_gzip(self):
        """The same envelope gzip Content-Encoded: identical observable
        results (the golden is byte-for-byte the simple_event golden except
        the recorded case name)."""
        await self._run_case(
            "simple_event_gzip",
            gzip.compress(self._fixture("simple_event.envelope")),
            extra_headers=[
                (b"origin", b"https://app.example"),
                (b"content-encoding", b"gzip"),
            ],
        )

    @unittest.skipUnless(zstd is not None, "zstd compressor requires Python 3.14+")
    async def test_simple_event_zstd(self):
        """The same envelope zstd Content-Encoded."""
        await self._run_case(
            "simple_event_zstd",
            zstd.compress(self._fixture("simple_event.envelope")),
            extra_headers=[
                (b"origin", b"https://app.example"),
                (b"content-encoding", b"zstd"),
            ],
        )

    async def test_transaction(self):
        """Transaction envelope. Timestamps must be current (old transactions
        are retention-dropped), so this is a builder with masked timestamps
        rather than a byte fixture."""
        now = timezone.now().isoformat()
        event = {
            "event_id": "f0e1d2c3b4a5968788796a5b4c3d2e1f",
            "type": "transaction",
            "transaction": "GET /parity-test",
            "contexts": {
                "trace": {
                    "trace_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                    "span_id": "aaaabbbbccccdddd",
                    "op": "http.server",
                }
            },
            "start_timestamp": now,
            "timestamp": now,
            "spans": [],
        }
        envelope = list_to_envelope(
            [
                {"event_id": "f0e1d2c3b4a5968788796a5b4c3d2e1f"},
                {"type": "transaction"},
                event,
            ]
        )
        await self._run_case(
            "transaction",
            envelope.encode(),
            masks=frozenset({"timestamp", "start_timestamp", "sent_at"}),
        )

    @unittest.skipUnless(settings.GLITCHTIP_ENABLE_LOGS, "logs feature disabled")
    async def test_log_items(self):
        """sentry-sdk log items envelope (GLITCHTIP_ENABLE_LOGS on, as in both
        CI lanes). Timestamps must be current, so a builder with masks."""
        payload = {
            "items": [
                {
                    "timestamp": time.time(),
                    "level": "warn",
                    "body": "parity log line",
                    "attributes": {
                        "sentry.service": {"value": "paritytest", "type": "string"}
                    },
                }
            ]
        }
        envelope = list_to_envelope([{}, {"type": "log"}, payload])
        await self._run_case(
            "log_items",
            envelope.encode(),
            masks=frozenset({"timestamp"}),
        )

    async def test_header_only_envelope(self):
        """An envelope that is just a header line — zero items."""
        await self._run_case("header_only", self._fixture("header_only.envelope"))

    async def test_header_dsn_only(self):
        """Credential only in the envelope-header ``dsn``, no query/header
        auth: the Rust pipeline recovers the key from the header and ingests.
        (The retired Python pipeline never read the body for auth and denied
        this — a divergence that was deliberate when both arms ran.)"""
        body = (
            self._fixture("header_dsn_only.envelope")
            .replace(b"__PUBLIC_KEY__", str(self.projectkey.public_key).encode())
            .replace(b"__PROJECT_ID__", str(self.project.id).encode())
        )
        await self._run_case("header_dsn_only", body, query="")

    async def test_oversize_body(self):
        """Body over DATA_UPLOAD_MAX_MEMORY_SIZE: the Rust pipeline answers
        413 mid-stream."""
        size = settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1
        body = (
            b'{"event_id": "cafe0d6be51c4b8a9f3e2d1c0b9a8f77"}\n'
            b'{"type": "attachment", "length": ' + str(size).encode() + b"}\n"
        )
        body += b"x" * (size - len(body))
        await self._run_case("oversize_body", body)

    # -- cases: junk and malformed bodies -------------------------------------

    async def test_over_unzipped_cap(self):
        """A body over GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE but under Django's
        DATA_UPLOAD_MAX_MEMORY_SIZE — the framing-time size cap, not the
        transport cap."""
        size = settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE + 1024
        self.assertLess(size, settings.DATA_UPLOAD_MAX_MEMORY_SIZE)
        body = (
            b'{"event_id": "beef0d6be51c4b8a9f3e2d1c0b9a8f77"}\n'
            b'{"type": "attachment", "length": ' + str(size).encode() + b"}\n"
        )
        body += b"x" * (size - len(body))
        await self._run_case("over_unzipped_cap", body)

    async def test_junk_body(self):
        """Not-JSON binary garbage where the envelope should be."""
        await self._run_case(
            "junk_body", b"\x00\x01\x02\xfe\xff not json at all \x80\x81\x00"
        )

    async def test_truncated_envelope(self):
        """Item header promises length=4096 but the payload is ~20 bytes."""
        await self._run_case("truncated", self._fixture("truncated.envelope"))

    async def test_unknown_item_type(self):
        """A well-formed item of a type neither supported nor ignored: the
        envelope is accepted (200), the item skipped, and the pipeline
        reports it as an ingest anomaly (asserted directly, same fingerprint
        contract as the fallback view's sentry capture)."""
        from unittest import mock

        with mock.patch("glitchtip.rust_ingest._report_anomalies") as report:
            await self._run_case("unknown_item", self._fixture("unknown_item.envelope"))
        anomalies = [a for call in report.call_args_list for a in call.args[0]]
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].get("kind"), "unsupported-item-type")
        self.assertEqual(anomalies[0].get("item_type"), "quantum_report")

    async def test_non_utf8_header(self):
        """Envelope whose header line is not valid UTF-8."""
        body = (
            b'\xff\xfe\x80{"event_id": "aa15a1d0f2f04df08a4d35d1cb8e5ec2"}\n'
            b'{"type": "event"}\n'
            b"{}\n"
        )
        await self._run_case("non_utf8_header", body)

    async def test_empty_body(self):
        """Zero-byte request body."""
        await self._run_case("empty_body", b"")

    # -- cases: auth and throttling -------------------------------------------

    async def test_unknown_key(self):
        """Well-formed but unknown DSN key."""
        await self._run_case(
            "unknown_key",
            self._fixture("simple_event.envelope"),
            query="sentry_key=00000000c0de4a3bb1b8f2a1cafe0000",
        )

    async def test_malformed_key(self):
        """DSN key that is not a UUID at all."""
        await self._run_case(
            "malformed_key",
            self._fixture("simple_event.envelope"),
            query="sentry_key=nope",
        )

    async def test_no_auth(self):
        """No query credential, no auth header, no envelope-header dsn."""
        await self._run_case(
            "no_auth", self._fixture("simple_event.envelope"), query=""
        )

    async def test_throttled_project(self):
        """A 100%-throttled organization answers 429 + Retry-After."""
        await self.organization.__class__.objects.filter(
            id=self.organization.id
        ).aupdate(event_throttle_rate=100)
        await self._run_case(
            "throttled_project", self._fixture("simple_event.envelope")
        )

    # -- cases: fallback -------------------------------------------------------

    async def test_minidump_envelope(self):
        """SDK minidump attachment: the Rust pipeline hands the buffered
        request back to the Python fallback view pre-side-effect. The 8-byte
        MDMP stub parses into a minimal fatal native event whose event_id
        (the envelope header has none, so one is generated) and timestamp
        are masked."""
        await self._run_case(
            "minidump",
            self._fixture("minidump.envelope"),
            masks=frozenset({"event_id", "timestamp"}),
        )
