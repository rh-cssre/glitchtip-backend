"""Tests for the Rust envelope-ingest ASGI path (``GLITCHTIP_RUST_INGEST``).

These drive ``IngestDispatcher`` at the ASGI layer — the seam where the Rust
path replaces the Django view — and then flush the vtasks batch so the real
worker tasks consume what Rust enqueued. That covers the full contract: DSN
auth against the shared Postgres pool, Valkey block/dedupe caches, the vtasks
wire format, and worker-side re-validation of the payloads.

Skipped unless ``GLITCHTIP_RUST_INGEST`` is enabled: the flag wires the Rust
ASGI dispatch in (the ``gt_rust.django_backend`` engine and gt_rust Valkey
cache driver it relies on run unconditionally), so these run in a dedicated
flag-on test lane while the default suite keeps covering the Python view.
"""

import importlib
import json
import unittest
import uuid

from django.conf import settings
from django.core.cache import cache
from django.tasks import task_backends

from apps.issue_events.models import IssueEvent, UserReport
from apps.logs.models import LogEvent
from apps.performance.models import TransactionGroup

from .utils import (
    CLIENT_IP,
    AsgiIngestTestMixin,
    EventIngestTestCase,
    list_to_envelope,
)


@unittest.skipUnless(
    settings.GLITCHTIP_RUST_INGEST, "requires GLITCHTIP_RUST_INGEST=true"
)
class RustIngestTestCase(AsgiIngestTestMixin, EventIngestTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        # Rust writes its block/dedupe/queue state to the dedicated test
        # Valkey database — start each test from a clean slate.
        from glitchtip.rust_ingest import _valkey_driver

        _valkey_driver().flushdb_sync()

    async def _drain_rust_queue(self):
        """Pop everything the Rust path enqueued and run it through the real
        task functions. Going through ``django_vtasks.serialization`` proves
        the Rust-written bytes are exactly what a worker would consume."""
        from django_vtasks.serialization import deserialize

        from glitchtip.rust_ingest import _valkey_driver

        driver = _valkey_driver()
        while True:
            raw = await driver.rpop("{vt}:q:ingest")
            if raw is None:
                break
            message = deserialize(bytes(raw))
            module, name = message["func"].rsplit(".", 1)
            vtask = getattr(importlib.import_module(module), name)
            # Ingest tasks are batched consumers: each call takes a list of
            # task-message dicts.
            await vtask.func([message])

    def _envelope(self, replace_id=True) -> tuple[list, str]:
        data = self.get_json_data(
            "apps/event_ingest/tests/test_data/envelopes/django_message.json"
        )
        if replace_id:
            new_id = uuid.uuid4().hex
            data[0]["event_id"] = new_id
            data[2]["event_id"] = new_id
        return data, list_to_envelope(data)

    async def test_event_envelope(self):
        data, envelope = self._envelope()
        status, headers, body = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], data[0]["event_id"])
        await self._drain_rust_queue()
        self.assertEqual(await self.project.issues.acount(), 1)
        event = await IssueEvent.objects.aget()
        self.assertEqual(event.event_id.hex, data[0]["event_id"])
        # Client IP was injected before enqueue — anonymized, because
        # project/org IP scrubbing is on by default (the /24 mask matches
        # anonymizeip's default exactly like the Python view).
        self.assertEqual(event.data.get("user", {}).get("ip_address"), "93.184.216.0")

    async def test_duplicate_event_id_ingested_once(self):
        _, envelope = self._envelope()
        await self._post(envelope.encode())
        status, _, _ = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        await self._drain_rust_queue()
        self.assertEqual(await IssueEvent.objects.acount(), 1)

    async def test_auth_rejections(self):
        _, envelope = self._envelope()
        # Unknown (but well-formed) key: Denied, and the rejection is cached.
        status, _, body = await self._post(
            envelope.encode(), query=f"sentry_key={uuid.uuid4().hex}"
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"detail": "Denied"})
        # Malformed key: Invalid DSN.
        status, _, body = await self._post(envelope.encode(), query="sentry_key=nope")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"detail": "Invalid DSN"})
        # No auth at all.
        status, _, body = await self._post(envelope.encode(), query="")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"detail": "Denied"})
        await self._drain_rust_queue()
        self.assertEqual(await IssueEvent.objects.acount(), 0)

    async def test_header_dsn_auth_no_query_credential(self):
        """Worst case: the credential is *only* in the envelope-header ``dsn`` —
        no query string, no auth header. The Python view denies this (it reads
        only the query / Authorization / X-Sentry-Auth); the Rust path recovers
        the public key from the header and accepts, so a mis-configured SDK's
        events are ingested instead of silently dropped."""
        data, _ = self._envelope()
        data[0]["dsn"] = (
            f"https://{self.projectkey.public_key}@localhost/{self.project.id}"
        )
        status, _, body = await self._post(list_to_envelope(data).encode(), query="")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], data[0]["event_id"])
        await self._drain_rust_queue()
        self.assertEqual(await IssueEvent.objects.acount(), 1)

    async def test_header_dsn_bad_key_denied(self):
        """A bogus key in the envelope-header DSN, with no query credential, is
        denied — the header path rejects junk, it does not wave it through."""
        data, _ = self._envelope()
        data[0]["dsn"] = f"https://{uuid.uuid4().hex}@localhost/{self.project.id}"
        status, _, body = await self._post(list_to_envelope(data).encode(), query="")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"detail": "Denied"})
        await self._drain_rust_queue()
        self.assertEqual(await IssueEvent.objects.acount(), 0)

    async def test_hard_throttle(self):
        await self.organization.__class__.objects.filter(
            id=self.organization.id
        ).aupdate(event_throttle_rate=100)
        _, envelope = self._envelope()
        status, headers, _ = await self._post(envelope.encode())
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("Retry-After"), "600")

    async def test_transaction_envelope(self):
        from django.utils import timezone

        now = timezone.now().isoformat()
        event = {
            "event_id": uuid.uuid4().hex,
            "type": "transaction",
            "transaction": "GET /rust-test",
            "contexts": {
                "trace": {
                    "trace_id": uuid.uuid4().hex,
                    "span_id": "aaaabbbbccccdddd",
                    "op": "http.server",
                }
            },
            "start_timestamp": now,
            "timestamp": now,
            "spans": [],
        }
        envelope = list_to_envelope(
            [{"event_id": uuid.uuid4().hex}, {"type": "transaction"}, event]
        )
        status, _, _ = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        await self._drain_rust_queue()
        self.assertEqual(await TransactionGroup.objects.acount(), 1)

    async def test_log_envelope(self):
        import time

        payload = {
            "items": [
                {
                    "timestamp": time.time(),
                    "level": "WARN",
                    "body": "rust ingest log",
                    "attributes": {
                        "sentry.service": {"value": "apitest", "type": "string"}
                    },
                }
            ]
        }
        envelope = list_to_envelope([{}, {"type": "log"}, payload])
        status, _, _ = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        await self._drain_rust_queue()
        log = await LogEvent.objects.aget()
        self.assertEqual(log.body, "rust ingest log")
        self.assertEqual(log.service, "apitest")

    async def test_user_report_envelope(self):
        report = {
            "event_id": uuid.uuid4().hex,
            "name": "n" * 200,
            "email": "reporter@example.com",
            "comments": "it broke",
        }
        envelope = list_to_envelope([{}, {"type": "user_report"}, report])
        status, _, _ = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        await self._drain_rust_queue()
        saved = await UserReport.objects.aget()
        self.assertEqual(saved.comments, "it broke")
        self.assertEqual(len(saved.name), 128)  # view-side truncation

    async def test_unknown_item_type_is_ignored(self):
        envelope = list_to_envelope([{}, {"type": "quantum_report"}, {"x": 1}])
        status, _, body = await self._post(envelope.encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        await self._drain_rust_queue()
        self.assertEqual(await IssueEvent.objects.acount(), 0)

    async def test_cors_header_on_response(self):
        _, envelope = self._envelope()
        status, headers, _ = await self._post(
            envelope.encode(), extra_headers=[(b"origin", b"https://app.example")]
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    async def test_non_post_falls_through_to_django(self):
        status, _, _ = await self._post(b"", method="GET")
        # The Django view answers non-POST envelope requests.
        self.assertEqual(status, 405)

    async def test_oversized_project_id_falls_through_to_django(self):
        # A project id past the BIGINT/i64 range can't name a real project
        # and must not overflow the Rust session arg into a 500 + error
        # capture; the Rust handler declines it and the Django path answers.
        _, envelope = self._envelope()
        status, _, _ = await self._post(
            envelope.encode(), path="/api/99999999999999999999999/envelope/"
        )
        self.assertNotEqual(status, 500)
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)

    async def test_minidump_envelope_falls_back_to_python_view(self):
        # A garbage minidump exercises the pre-side-effect fallback: the
        # Rust path hands the buffered request to the Django view, which
        # owns the minidump -> event conversion.
        envelope = (
            b"{}\n"
            b'{"type": "attachment", "attachment_type": "event.minidump", "length": 8}\n'
            b"MDMPxxxx\n"
        )
        status, _, body = await self._post(envelope)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

    async def test_python_rust_payload_parity(self):
        """The worker-visible event must not depend on which path served the
        request: post the same event through the Rust ASGI path and the
        Python view, then compare the stored events field by field."""
        from asgiref.sync import sync_to_async

        data, envelope = self._envelope()
        await self._post(envelope.encode())

        rust_id = data[0]["event_id"]
        python_id = uuid.uuid4().hex
        data[0]["event_id"] = python_id
        data[2]["event_id"] = python_id

        def post_python():
            return self.client.post(
                self.url_for_python_view(),
                list_to_envelope(data),
                content_type="application/json",
                REMOTE_ADDR=CLIENT_IP,
            )

        res = await sync_to_async(post_python)()
        self.assertEqual(res.status_code, 200)
        await self._drain_rust_queue()
        # The Python view enqueued via the immediate backend; its flush runs
        # async task functions through async_to_sync, so it needs a thread.
        await sync_to_async(task_backends["default"].flush_batches)()

        rust_event = await IssueEvent.objects.aget(event_id=rust_id)
        python_event = await IssueEvent.objects.aget(event_id=python_id)
        self.assertEqual(rust_event.data, python_event.data)
        self.assertEqual(rust_event.title, python_event.title)
        self.assertEqual(rust_event.type, python_event.type)
        self.assertEqual(rust_event.level, python_event.level)
        self.assertEqual(rust_event.tags, python_event.tags)
        # Both landed in the same issue (identical grouping hash).
        self.assertEqual(rust_event.issue_id, python_event.issue_id)

    def url_for_python_view(self):
        from django.urls import reverse

        return reverse("event_envelope", args=[self.project.id]) + self.params
