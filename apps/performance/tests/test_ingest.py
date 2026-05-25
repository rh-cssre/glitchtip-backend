"""
End-to-end tests for transaction ingest → TransactionGroup stats + SpanStaging.
"""

from datetime import datetime, timedelta

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.event_ingest.process_event import process_transaction_events
from apps.event_ingest.schema import InterchangeTransactionEvent, TransactionEventSchema
from apps.event_ingest.tests.utils import run_async_closing
from apps.performance.models import SpanStaging, TransactionGroup


def _process_transaction_events(*args, **kwargs):
    return run_async_closing(process_transaction_events, *args, **kwargs)


def _make_transaction_payload(
    transaction_name: str = "/api/test/",
    op: str = "http.server",
    method: str = "GET",
    duration_ms: float = 100.0,
    trace_status: str = "ok",
    spans: list | None = None,
    base_time: datetime | None = None,
):
    """Build a TransactionEventSchema-compatible dict."""
    if base_time is None:
        base_time = timezone.now() - timedelta(minutes=1)
    start = base_time
    end = start + timedelta(milliseconds=duration_ms)

    # Generate span timestamps relative to the transaction start
    resolved_spans = []
    if spans:
        for span in spans:
            s = dict(span)
            # Convert relative span timestamps to absolute if they look old
            if "start_timestamp" not in s:
                s["start_timestamp"] = start.isoformat()
            if "timestamp" not in s:
                s["timestamp"] = end.isoformat()
            resolved_spans.append(s)

    payload = {
        "type": "transaction",
        "transaction": transaction_name,
        "start_timestamp": start.isoformat(),
        "timestamp": end.isoformat(),
        "contexts": {
            "trace": {
                "trace_id": "a" * 32,
                "span_id": "b" * 16,
                "op": op,
                "status": trace_status,
            }
        },
        "request": {"method": method, "url": f"http://localhost{transaction_name}"},
        "spans": resolved_spans,
        "platform": "python",
    }
    return payload


def _make_interchange_event(
    project_id: int,
    organization_id: int,
    payload_dict: dict,
) -> InterchangeTransactionEvent:
    """Build an InterchangeTransactionEvent from a payload dict."""
    return InterchangeTransactionEvent(
        project_id=project_id,
        organization_id=organization_id,
        received=timezone.now(),
        payload=TransactionEventSchema(**payload_dict),
    )


class TransactionIngestTestCase(TransactionTestCase):
    def setUp(self):
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.organization = self.project.organization

    def _ingest(self, payloads: list[dict]) -> None:
        events = [
            _make_interchange_event(self.project.id, self.organization.id, p)
            for p in payloads
        ]
        _process_transaction_events(events)

    def test_single_transaction_creates_group(self):
        """A single transaction creates a TransactionGroup with correct stats."""
        payload = _make_transaction_payload(duration_ms=150.0)
        self._ingest([payload])

        self.assertEqual(TransactionGroup.objects.count(), 1)
        group = TransactionGroup.objects.first()
        self.assertEqual(group.transaction, "/api/test/")
        self.assertEqual(group.op, "http.server")
        self.assertEqual(group.method, "GET")
        self.assertEqual(group.count, 1)
        self.assertAlmostEqual(group.avg_duration, 150.0, places=1)
        self.assertEqual(group.error_count, 0)
        self.assertIsNotNone(group.duration_histogram)
        self.assertGreater(sum(group.duration_histogram), 0)

    def test_multiple_transactions_accumulate(self):
        """Multiple transactions for the same endpoint accumulate stats."""
        payloads = [
            _make_transaction_payload(duration_ms=100.0),
            _make_transaction_payload(duration_ms=200.0),
            _make_transaction_payload(duration_ms=300.0),
        ]
        self._ingest(payloads)

        self.assertEqual(TransactionGroup.objects.count(), 1)
        group = TransactionGroup.objects.first()
        self.assertEqual(group.count, 3)
        # Running average: (100 + 200 + 300) / 3 = 200
        self.assertAlmostEqual(group.avg_duration, 200.0, places=1)
        self.assertEqual(sum(group.duration_histogram), 3)

    def test_incremental_batches_merge_correctly(self):
        """Stats from separate batches merge without corruption."""
        # Batch 1: two fast transactions
        self._ingest(
            [
                _make_transaction_payload(duration_ms=50.0),
                _make_transaction_payload(duration_ms=50.0),
            ]
        )
        group = TransactionGroup.objects.first()
        self.assertEqual(group.count, 2)
        self.assertAlmostEqual(group.avg_duration, 50.0, places=1)

        # Batch 2: one slow transaction
        self._ingest([_make_transaction_payload(duration_ms=200.0)])
        group.refresh_from_db()
        self.assertEqual(group.count, 3)
        # Running avg: (50*2 + 200) / 3 = 100
        self.assertAlmostEqual(group.avg_duration, 100.0, places=1)
        self.assertEqual(sum(group.duration_histogram), 3)

    def test_error_status_increments_error_count(self):
        """Trace statuses indicating errors increment error_count."""
        payloads = [
            _make_transaction_payload(trace_status="ok"),
            _make_transaction_payload(trace_status="internal_error"),
            _make_transaction_payload(trace_status="deadline_exceeded"),
            _make_transaction_payload(trace_status="ok"),
        ]
        self._ingest(payloads)

        group = TransactionGroup.objects.first()
        self.assertEqual(group.count, 4)
        self.assertEqual(group.error_count, 2)
        self.assertEqual(group.error_rate, 50.0)

    def test_p50_p95_computed(self):
        """p50 and p95 are computed from the histogram after ingest."""
        # 90 fast + 10 slow → p50 near 10ms, p95 near 5000ms
        payloads = [_make_transaction_payload(duration_ms=10.0)] * 90 + [
            _make_transaction_payload(duration_ms=5000.0)
        ] * 10
        self._ingest(payloads)

        group = TransactionGroup.objects.first()
        self.assertEqual(group.count, 100)
        self.assertIsNotNone(group.p50)
        self.assertIsNotNone(group.p95)
        self.assertLess(group.p50, 100)  # p50 should be near 10ms
        self.assertGreater(group.p95, 1000)  # p95 should be near 5000ms
        self.assertGreater(group.p95, group.p50)

    def test_different_endpoints_create_separate_groups(self):
        """Different transaction names create separate groups."""
        self._ingest(
            [
                _make_transaction_payload(transaction_name="/api/users/"),
                _make_transaction_payload(transaction_name="/api/projects/"),
            ]
        )
        self.assertEqual(TransactionGroup.objects.count(), 2)

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="true")
    def test_spans_written_to_staging(self):
        """Spans from the transaction are written to SpanStaging."""
        base = timezone.now() - timedelta(minutes=1)
        spans = [
            {
                "trace_id": "a" * 32,
                "span_id": "c" * 16,
                "parent_span_id": "b" * 16,
                "op": "db",
                "description": "SELECT * FROM users WHERE id = 1",
                "start_timestamp": (base + timedelta(milliseconds=10)).isoformat(),
                "timestamp": (base + timedelta(milliseconds=25)).isoformat(),
            },
            {
                "trace_id": "a" * 32,
                "span_id": "d" * 16,
                "parent_span_id": "b" * 16,
                "op": "http.client",
                "description": "GET https://api.example.com/data",
                "start_timestamp": (base + timedelta(milliseconds=30)).isoformat(),
                "timestamp": (base + timedelta(milliseconds=80)).isoformat(),
            },
        ]
        payload = _make_transaction_payload(spans=spans, base_time=base)
        self._ingest([payload])

        staging_rows = SpanStaging.objects.all()
        self.assertEqual(staging_rows.count(), 2)

        db_span = staging_rows.filter(op="db").first()
        self.assertIsNotNone(db_span)
        self.assertEqual(db_span.transaction_name, "/api/test/")
        self.assertEqual(db_span.organization_id, self.organization.id)
        self.assertEqual(db_span.project_id, self.project.id)
        # Duration should be ~15ms
        self.assertAlmostEqual(db_span.duration, 15.0, places=0)
        # SQL should be parameterized
        self.assertIn("%s", db_span.description)

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="true")
    def test_span_description_parameterized(self):
        """SQL literals in span descriptions are replaced with %s."""
        base = timezone.now() - timedelta(minutes=1)
        spans = [
            {
                "trace_id": "a" * 32,
                "span_id": "c" * 16,
                "parent_span_id": "b" * 16,
                "op": "db",
                "description": "SELECT * FROM users WHERE name = 'alice' AND id = 42",
                "start_timestamp": (base + timedelta(milliseconds=10)).isoformat(),
                "timestamp": (base + timedelta(milliseconds=20)).isoformat(),
            },
        ]
        payload = _make_transaction_payload(spans=spans, base_time=base)
        self._ingest([payload])

        span = SpanStaging.objects.first()
        # String literal 'alice' and numeric 42 should both be %s
        self.assertNotIn("alice", span.description)
        self.assertNotIn("42", span.description)
        self.assertIn("%s", span.description)

    def test_organization_set_on_group(self):
        """TransactionGroup gets the correct organization_id from ingest."""
        self._ingest([_make_transaction_payload()])
        group = TransactionGroup.objects.first()
        self.assertEqual(group.organization_id, self.organization.id)
