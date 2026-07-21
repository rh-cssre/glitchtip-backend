"""
Tests for log ingestion pipeline.
"""

import json
import time
from datetime import datetime, timezone
from unittest import mock

from django.core.cache import cache
from django.db.utils import IntegrityError
from django.tasks import task_backends
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django_async_backend.db import async_connections
from model_bakery import baker

from apps.event_ingest.tests.utils import fake_integrity_error, run_async_closing
from apps.shared.raw_sql import copy_rows, is_unique_violation
from glitchtip.partition_manager import UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import LogLevel
from ..models import LogEvent
from ..process_logs import LEVEL_MAP, parse_span_id
from ..process_logs import process_log_events as _aprocess_log_events
from ..tasks import LogTaskMessage


# ``process_log_events`` became async in the django-async-backend
# integration. Tests still drive it synchronously; ``run_async_closing``
# wraps async_to_sync with a teardown that closes the task-local async
# DB connection before the task ends, so we don't leak sockets across
# tests.
def process_log_events(*args, **kwargs):
    return run_async_closing(_aprocess_log_events, *args, **kwargs)


def list_to_envelope(data: list[dict]) -> str:
    """Convert list of dicts to newline-delimited JSON envelope format."""
    return "\n".join([json.dumps(item) for item in data])


class OtelLogConversionTestCase(TestCase):
    """Test OTel log record to LogItemSchema conversion."""

    def test_basic_conversion(self):
        from apps.event_ingest.schema import otel_log_to_log_item

        now_ns = str(int(time.time() * 1e9))
        otel_record = {
            "severity_text": "error",
            "severity_number": 17,
            "body": {"string_value": "Disk space low"},
            "time_unix_nano": now_ns,
            "trace_id": "edec519707974fc8bfccb5a017e17394",
            "span_id": "b01c6992ac861a7d",
        }
        result = otel_log_to_log_item(otel_record)
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["body"], "Disk space low")
        self.assertAlmostEqual(result["timestamp"], int(now_ns) / 1e9, places=2)
        self.assertEqual(result["trace_id"], "edec519707974fc8bfccb5a017e17394")
        self.assertEqual(result["span_id"], "b01c6992ac861a7d")
        self.assertEqual(result["severity_number"], 17)

    def test_camel_case_fields(self):
        """OTel JSON uses camelCase field names."""
        from apps.event_ingest.schema import otel_log_to_log_item

        now_ns = str(int(time.time() * 1e9))
        otel_record = {
            "severityText": "debug",
            "severityNumber": 5,
            "body": {"string_value": "Startup"},
            "timeUnixNano": now_ns,
            "traceId": "aaaa519707974fc8bfccb5a017e17394",
            "spanId": "0123456789abcdef",
        }
        result = otel_log_to_log_item(otel_record)
        self.assertEqual(result["level"], "debug")
        self.assertEqual(result["trace_id"], "aaaa519707974fc8bfccb5a017e17394")
        self.assertEqual(result["span_id"], "0123456789abcdef")

    def test_list_attributes(self):
        from apps.event_ingest.schema import otel_log_to_log_item

        otel_record = {
            "severity_number": 9,
            "body": {"string_value": "test"},
            "time_unix_nano": str(int(time.time() * 1e9)),
            "attributes": [
                {"key": "service.name", "value": {"string_value": "web"}},
                {"key": "count", "value": {"int_value": 42}},
            ],
        }
        result = otel_log_to_log_item(otel_record)
        # service.name maps to the service field via LogItemSchema
        self.assertEqual(result["attributes"]["service.name"]["value"], "web")
        self.assertEqual(result["attributes"]["count"]["value"], 42)

    def test_missing_body(self):
        from apps.event_ingest.schema import otel_log_to_log_item

        result = otel_log_to_log_item({"time_unix_nano": str(int(time.time() * 1e9))})
        self.assertEqual(result["body"], "")
        self.assertEqual(result["level"], "info")


class LogItemSchemaCompatTestCase(TestCase):
    """Test LogItemSchema compatibility with various SDK payload shapes."""

    def test_iso_timestamp_string(self):
        """sentry.dart sends ISO-8601 timestamp strings."""
        from apps.event_ingest.schema import LogItemSchema

        item = LogItemSchema(
            timestamp="2026-04-03T02:57:11.646571Z",
            level="info",
            body="hello",
        )
        # 2026-04-03T02:57:11.646571Z → unix seconds
        self.assertAlmostEqual(item.timestamp, 1775185031.646571, places=3)

    def test_iso_timestamp_with_offset(self):
        from apps.event_ingest.schema import LogItemSchema

        # 2026-04-02T16:49:41.8508431+08:00 == 2026-04-02T08:49:41.8508431Z
        item = LogItemSchema(
            timestamp="2026-04-02T16:49:41.8508431+08:00",
            level="info",
            body="hello",
        )
        self.assertAlmostEqual(item.timestamp, 1775119781.850843, places=3)

    def test_float_timestamp_still_works(self):
        from apps.event_ingest.schema import LogItemSchema

        item = LogItemSchema(timestamp=1775203281.699, level="info", body="hi")
        self.assertEqual(item.timestamp, 1775203281.699)


class LogIngestProcessingTestCase(TransactionTestCase):
    """Test log processing function"""

    def setUp(self):
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.organization = self.project.organization

    LOG_EVENT_COLUMNS = [
        "id",
        "trace_id",
        "organization_id",
        "project_id",
        "span_id",
        "level",
        "severity_number",
        "body",
        "service",
        "environment",
        "host",
        "data",
    ]

    def _log_message(self, body: str) -> LogTaskMessage:
        now = datetime.now(timezone.utc)
        return LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[{"timestamp": now.timestamp(), "level": "info", "body": body}],
        )

    def test_copy_fallback_on_duplicate(self):
        """A unique-violation COPY falls back to the conflict-tolerant INSERT."""
        with mock.patch(
            "apps.logs.process_logs.copy_rows",
            side_effect=fake_integrity_error("23505"),
        ) as copy_mock:
            count = process_log_events([self._log_message("dup")])
        copy_mock.assert_called_once()
        self.assertEqual(count, 1)
        self.assertEqual(LogEvent.objects.count(), 1)

    def test_copy_non_unique_integrity_error_propagates(self):
        """A non-conflict IntegrityError (e.g. a missing partition, 23514)
        must not retry through the INSERT — it would fail identically."""
        with mock.patch(
            "apps.logs.process_logs.copy_rows",
            side_effect=fake_integrity_error("23514"),
        ):
            with self.assertRaises(IntegrityError):
                process_log_events([self._log_message("gap")])
        self.assertEqual(LogEvent.objects.count(), 0)

    def test_copy_rows_with_debug_cursor(self):
        """copy_rows works under the debug cursor (DEBUG=True dev setups),
        whose ``copy`` override is an async generator for COPY TO reads."""
        row = (
            str(UUID7Helper.from_datetime()),
            None,
            self.organization.id,
            self.project.id,
            None,
            int(LogLevel.INFO),
            None,
            "debug cursor body",
            "",
            "",
            "",
            "{}",
        )

        async def run():
            conn = async_connections["default"]
            conn.force_debug_cursor = True
            try:
                return await copy_rows("logs_logevent", self.LOG_EVENT_COLUMNS, [row])
            finally:
                conn.force_debug_cursor = False

        count = run_async_closing(run)
        self.assertEqual(count, 1)
        self.assertEqual(LogEvent.objects.count(), 1)

    def test_copy_rows_duplicate_raises_integrity_error(self):
        """copy_rows surfaces primary-key conflicts as Django's IntegrityError."""
        row = (
            str(UUID7Helper.from_datetime()),
            None,
            self.organization.id,
            self.project.id,
            None,
            int(LogLevel.INFO),
            None,
            "copied body",
            "",
            "",
            "",
            "{}",
        )
        run_async_closing(copy_rows, "logs_logevent", self.LOG_EVENT_COLUMNS, [row])
        self.assertEqual(LogEvent.objects.count(), 1)
        with self.assertRaises(IntegrityError) as ctx:
            run_async_closing(copy_rows, "logs_logevent", self.LOG_EVENT_COLUMNS, [row])
        self.assertEqual(LogEvent.objects.count(), 1)
        # The chained driver exception carries the psycopg-shaped sqlstate
        # the COPY fallback keys on — the cross-driver contract
        # is_unique_violation() relies on.
        self.assertTrue(is_unique_violation(ctx.exception))
        self.assertEqual(ctx.exception.__cause__.sqlstate, "23505")

    def test_is_unique_violation_requires_structured_sqlstate(self):
        """Only a structured ``sqlstate`` of 23505 matches. Exceptions
        without the attribute or with ``sqlstate=None`` (client-side
        errors) fail closed — untrusted text mimicking the old [SQLSTATE]
        message-prefix fallback must not be trusted."""
        no_attr = IntegrityError("duplicate key")
        no_attr.__cause__ = Exception("[23505] duplicate key value")
        self.assertFalse(is_unique_violation(no_attr))
        partition = IntegrityError("no partition")
        partition.__cause__ = Exception("[23514] no partition of relation")
        self.assertFalse(is_unique_violation(partition))
        client_side = IntegrityError("client-side")
        cause = Exception("[23505] attacker-controlled text")
        cause.sqlstate = None
        client_side.__cause__ = cause
        self.assertFalse(is_unique_violation(client_side))
        self.assertFalse(is_unique_violation(IntegrityError("no cause")))

    def test_process_single_log(self):
        """Test processing a single log event"""
        now = datetime.now(timezone.utc)
        timestamp = now.timestamp()

        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {
                    "timestamp": timestamp,
                    "level": "info",
                    "body": "Test log message",
                    "service": "test-svc",
                    "environment": "prod",
                    "host": "web-1",
                }
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 1)
        self.assertEqual(LogEvent.objects.count(), 1)

        log = LogEvent.objects.first()
        self.assertEqual(log.body, "Test log message")
        self.assertEqual(log.level, LogLevel.INFO)
        self.assertEqual(log.organization_id, self.organization.id)
        self.assertEqual(log.project_id, self.project.id)
        self.assertEqual(log.service, "test-svc")
        self.assertEqual(log.environment, "prod")
        self.assertEqual(log.host, "web-1")

    def test_process_multiple_logs(self):
        """Test processing multiple log events in one batch"""
        now = datetime.now(timezone.utc)
        timestamp = now.timestamp()

        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {"timestamp": timestamp, "level": "info", "body": "Log 1"},
                {"timestamp": timestamp, "level": "warn", "body": "Log 2"},
                {"timestamp": timestamp, "level": "error", "body": "Log 3"},
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 3)
        self.assertEqual(LogEvent.objects.count(), 3)

    def test_process_log_with_trace_id(self):
        """Test processing log with trace ID"""
        now = datetime.now(timezone.utc)
        timestamp = now.timestamp()
        trace_id = "550e8400-e29b-41d4-a716-446655440000"

        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {
                    "timestamp": timestamp,
                    "level": "info",
                    "body": "Test log with trace",
                    "trace_id": trace_id,
                }
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 1)
        log = LogEvent.objects.first()
        self.assertEqual(str(log.trace_id), trace_id)

    def test_level_mapping(self):
        """Test all level mappings are correct"""
        self.assertEqual(LEVEL_MAP["trace"], LogLevel.TRACE)
        self.assertEqual(LEVEL_MAP["debug"], LogLevel.DEBUG)
        self.assertEqual(LEVEL_MAP["info"], LogLevel.INFO)
        self.assertEqual(LEVEL_MAP["warn"], LogLevel.WARN)
        self.assertEqual(LEVEL_MAP["warning"], LogLevel.WARN)
        self.assertEqual(LEVEL_MAP["error"], LogLevel.ERROR)
        self.assertEqual(LEVEL_MAP["fatal"], LogLevel.FATAL)

    def test_process_log_with_severity_number(self):
        """Test processing log with OpenTelemetry severity number"""
        now = datetime.now(timezone.utc)
        timestamp = now.timestamp()

        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {
                    "timestamp": timestamp,
                    "level": "info",
                    "body": "Test log",
                    "severity_number": 9,  # OTel INFO
                }
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 1)
        log = LogEvent.objects.first()
        self.assertEqual(log.severity_number, 9)

    def test_parse_span_id_high_bit(self):
        """Span IDs with the high bit set must fit in signed bigint."""
        # This span_id caused NumericValueOutOfRange in production:
        # 0xb1392b5a6c42881e = 12770285885749102622 > bigint max (2^63-1)
        result = parse_span_id("b1392b5a6c42881e")
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, -(1 << 63))
        self.assertLess(result, 1 << 63)

    def test_process_log_with_high_bit_span_id(self):
        """Logs with high-bit span_id should insert without overflow."""
        now = datetime.now(timezone.utc)
        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {
                    "timestamp": now.timestamp(),
                    "level": "info",
                    "body": "Test log with high-bit span_id",
                    "span_id": "b1392b5a6c42881e",
                }
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 1)
        log = LogEvent.objects.first()
        self.assertIsNotNone(log.span_id)

    def test_process_log_preserves_extra_data(self):
        """Test that extra fields are preserved in data"""
        now = datetime.now(timezone.utc)
        timestamp = now.timestamp()

        message = LogTaskMessage(
            project_id=self.project.id,
            organization_id=self.organization.id,
            received=now,
            logs=[
                {
                    "timestamp": timestamp,
                    "level": "info",
                    "body": "Test log",
                    "custom_field": "custom_value",
                    "request_id": "abc-123",
                }
            ],
        )

        count = process_log_events([message])

        self.assertEqual(count, 1)
        log = LogEvent.objects.first()
        self.assertEqual(log.data["custom_field"], "custom_value")
        self.assertEqual(log.data["request_id"], "abc-123")


@override_settings(GLITCHTIP_ENABLE_LOGS=True)
class LogEnvelopeAPITestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test log ingestion via envelope API"""

    def setUp(self):
        self.create_project()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_envelope", args=[self.project.id]) + self.params
        cache.clear()

    def test_log_envelope_accepted(self):
        """Test that log envelope items are accepted"""
        now = time.time()
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440000",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "log", "item_count": 2},
            {
                "items": [
                    {"timestamp": now, "level": "info", "body": "Test log 1"},
                    {"timestamp": now, "level": "warn", "body": "Test log 2"},
                ]
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_log_envelope_with_trace_id(self):
        """Test log envelope with trace correlation"""
        now = time.time()
        trace_id = "550e8400e29b41d4a716446655440000"
        envelope_data = [
            {"event_id": trace_id, "sent_at": "2024-01-01T00:00:00Z"},
            {"type": "log", "item_count": 1},
            {
                "items": [
                    {
                        "timestamp": now,
                        "level": "info",
                        "body": "Test log with trace",
                        "trace_id": trace_id,
                    }
                ]
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 1)

        log = LogEvent.objects.first()
        self.assertEqual(str(log.trace_id).replace("-", ""), trace_id)

    def test_mixed_envelope_items(self):
        """Test envelope with both event and log items"""
        now = time.time()
        event_id = "550e8400e29b41d4a716446655440001"
        envelope_data = [
            {"event_id": event_id, "sent_at": "2024-01-01T00:00:00Z"},
            {"type": "log", "item_count": 1},
            {
                "items": [
                    {"timestamp": now, "level": "info", "body": "Test log"},
                ]
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        # Log should be created
        self.assertEqual(LogEvent.objects.count(), 1)

    def test_log_envelope_preserves_extra_attributes(self):
        """Test that arbitrary SDK attributes survive schema validation into JSONB data."""
        now = time.time()
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440002",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "log", "item_count": 1},
            {
                "items": [
                    {
                        "timestamp": now,
                        "level": "info",
                        "body": "Test log with extras",
                        "sentry.message.template": "Hello %s",
                        "custom.user_id": "u-42",
                    },
                ]
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 1)
        log = LogEvent.objects.first()
        self.assertEqual(log.data["custom.user_id"], "u-42")

    def test_otel_log_envelope_accepted(self):
        """Test that otel_log envelope items are accepted and converted."""
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440010",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "otel_log"},
            # OTel log data model: https://opentelemetry.io/docs/specs/otel/logs/data-model/
            {
                "severity_text": "info",
                "severity_number": 9,
                "body": {"string_value": "Application started successfully"},
                "time_unix_nano": str(int(time.time() * 1e9)),
                "trace_id": "edec519707974fc8bfccb5a017e17394",
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        backend = task_backends["default"]
        backend.flush_batches()
        backend.flush_batches()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(LogEvent.objects.count(), 1, "No LogEvent created")
        log = LogEvent.objects.first()
        self.assertEqual(log.body, "Application started successfully")
        self.assertEqual(log.level, LogLevel.INFO)
        self.assertEqual(log.severity_number, 9)
        self.assertEqual(
            str(log.trace_id).replace("-", ""), "edec519707974fc8bfccb5a017e17394"
        )

    def test_otel_log_severity_mapping(self):
        """Test OTel severity_number to level mapping per OTel spec."""
        test_cases = [
            (1, "trace"),
            (4, "trace"),
            (5, "debug"),
            (8, "debug"),
            (9, "info"),
            (12, "info"),
            (13, "warn"),
            (16, "warn"),
            (17, "error"),
            (20, "error"),
            (21, "fatal"),
            (24, "fatal"),
        ]
        for severity_number, expected_level in test_cases:
            LogEvent.objects.all().delete()
            envelope_data = [
                {
                    "event_id": "550e8400e29b41d4a716446655440011",
                    "sent_at": "2024-01-01T00:00:00Z",
                },
                {"type": "otel_log"},
                {
                    "severity_number": severity_number,
                    "body": {"string_value": f"Test level {severity_number}"},
                    "time_unix_nano": str(int(time.time() * 1e9)),
                },
            ]
            res = self.client.post(
                self.url,
                list_to_envelope(envelope_data),
                content_type="application/json",
            )
            task_backends["default"].flush_batches()
            self.assertEqual(res.status_code, 200)
            log = LogEvent.objects.first()
            self.assertEqual(
                log.level,
                getattr(LogLevel, expected_level.upper()),
                f"severity_number={severity_number} should map to {expected_level}",
            )

    def test_otel_log_with_attributes(self):
        """Test OTel log with list-of-dicts attributes format."""
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440012",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "otel_log"},
            {
                "severity_text": "warn",
                "severity_number": 13,
                "body": {"string_value": "Connection pool exhausted"},
                "time_unix_nano": str(int(time.time() * 1e9)),
                "attributes": [
                    {
                        "key": "service.name",
                        "value": {"string_value": "api-gateway"},
                    },
                    {
                        "key": "deployment.environment.name",
                        "value": {"string_value": "staging"},
                    },
                    {
                        "key": "pool.size",
                        "value": {"int_value": 10},
                    },
                ],
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 1)
        log = LogEvent.objects.first()
        self.assertEqual(log.body, "Connection pool exhausted")
        self.assertEqual(log.level, LogLevel.WARN)
        self.assertEqual(log.service, "api-gateway")
        self.assertEqual(log.environment, "staging")
        self.assertEqual(log.data["pool.size"], 10)

    def test_otel_log_multiple_items_in_envelope(self):
        """Test multiple otel_log items in a single envelope."""
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440013",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "otel_log"},
            {
                "severity_number": 9,
                "body": {"string_value": "Log one"},
                "time_unix_nano": str(int(time.time() * 1e9)),
            },
            {"type": "otel_log"},
            {
                "severity_number": 17,
                "body": {"string_value": "Log two"},
                "time_unix_nano": str(int(time.time() * 1e9)),
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_log_envelope_normalizes_sdk_attributes(self):
        """Test that SDK attributes dict is normalized into top-level fields."""
        now = time.time()
        envelope_data = [
            {
                "event_id": "550e8400e29b41d4a716446655440003",
                "sent_at": "2024-01-01T00:00:00Z",
            },
            {"type": "log", "item_count": 1},
            {
                "items": [
                    {
                        "timestamp": now,
                        "level": "info",
                        "body": "SDK-format test",
                        "attributes": {
                            "sentry.service": {
                                "value": "auth-service",
                                "type": "string",
                            },
                            "sentry.environment": {
                                "value": "production",
                                "type": "string",
                            },
                            "host.name": {"value": "web-1", "type": "string"},
                            "sentry.severity_number": {
                                "value": 9,
                                "type": "integer",
                            },
                            "sentry.severity_text": {
                                "value": "info",
                                "type": "string",
                            },
                            "custom.user_id": {
                                "value": "u-42",
                                "type": "string",
                            },
                        },
                    },
                ]
            },
        ]

        res = self.client.post(
            self.url,
            list_to_envelope(envelope_data),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(LogEvent.objects.count(), 1)
        log = LogEvent.objects.first()
        # Known attributes extracted to top-level fields
        self.assertEqual(log.service, "auth-service")
        self.assertEqual(log.environment, "production")
        self.assertEqual(log.host, "web-1")
        self.assertEqual(log.severity_number, 9)
        # Custom attributes stored as flat values in data JSONB
        self.assertEqual(log.data["custom.user_id"], "u-42")
        # sentry.severity_text is consumed, not stored
        self.assertNotIn("sentry.severity_text", log.data)
