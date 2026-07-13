"""Tests for native OTLP/HTTP logs ingest (``POST /v1/logs``).

These exercise the vanilla-OpenTelemetry path: protobuf and JSON payloads
posted directly by an OTel exporter (no sentry envelope), authenticated by the
DSN public key in an auth header. Payloads are built with the official
opentelemetry-proto messages — the same wire format an OTel SDK or the
Collector emits.
"""

import gzip
import time
from uuid import UUID

import orjson
from django.core.cache import cache
from django.tasks import task_backends
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs

from apps.event_ingest.constants import EVENT_BLOCK_CACHE_KEY
from apps.event_ingest.otlp import decode_otlp_logs
from apps.logs.constants import LogLevel
from apps.logs.models import LogEvent
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


def _str_attr(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _build_batch_request(record_count: int) -> bytes:
    """Build a single-resource, single-scope request with ``record_count``
    records — the shape an auto-instrumented app emits for a burst of logs."""
    now_ns = int(time.time() * 1e9)
    records = [
        LogRecord(
            time_unix_nano=now_ns,
            severity_number=9,  # INFO
            body=AnyValue(string_value=f"log number {i}"),
        )
        for i in range(record_count)
    ]
    return ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource={
                    "attributes": [
                        _str_attr("service.name", "batch-svc"),
                        _str_attr("deployment.environment.name", "prod"),
                        _str_attr("telemetry.sdk.version", "1.42.1"),
                    ]
                },
                scope_logs=[ScopeLogs(log_records=records)],
            )
        ]
    ).SerializeToString()


def _build_protobuf_request() -> bytes:
    """Build an OTLP ExportLogsServiceRequest with resource attributes and two
    log records — one correlated to a trace/span, one not."""
    now_ns = int(time.time() * 1e9)
    record_warn = LogRecord(
        time_unix_nano=now_ns,
        severity_number=13,  # WARN
        severity_text="WARN",
        body=AnyValue(string_value="Connection pool exhausted"),
        attributes=[
            KeyValue(key="pool.size", value=AnyValue(int_value=10)),
        ],
        trace_id=bytes.fromhex("edec519707974fc8bfccb5a017e17394"),
        span_id=bytes.fromhex("b01c6992ac861a7d"),
    )
    record_info = LogRecord(
        time_unix_nano=now_ns,
        severity_number=9,  # INFO
        severity_text="INFO",
        body=AnyValue(string_value="Application started"),
    )
    return ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource={
                    "attributes": [
                        _str_attr("service.name", "api-gateway"),
                        _str_attr("deployment.environment.name", "staging"),
                        _str_attr("host.name", "web-1"),
                        # SDK self-description — should be dropped, not stored.
                        _str_attr("telemetry.sdk.version", "1.42.1"),
                    ]
                },
                scope_logs=[
                    ScopeLogs(log_records=[record_warn, record_info]),
                ],
            )
        ]
    ).SerializeToString()


@override_settings(GLITCHTIP_ENABLE_LOGS=True)
class OTLPLogsIngestTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    def setUp(self):
        self.create_project()
        self.url = reverse("otlp_logs")
        self.auth = f"Bearer {self.projectkey.public_key.hex}"
        cache.clear()

    def _flush(self):
        backend = task_backends["default"]
        backend.flush_batches()
        backend.flush_batches()

    def test_protobuf_logs_ingested(self):
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self._flush()

        self.assertEqual(LogEvent.objects.count(), 2)
        warn = LogEvent.objects.get(level=LogLevel.WARN)
        self.assertEqual(warn.body, "Connection pool exhausted")
        # Resource attributes hoisted onto the record.
        self.assertEqual(warn.service, "api-gateway")
        self.assertEqual(warn.environment, "staging")
        self.assertEqual(warn.host, "web-1")
        self.assertEqual(warn.severity_number, 13)
        # Record attribute preserved in JSONB data.
        self.assertEqual(warn.data["pool.size"], 10)
        # SDK self-description dropped.
        self.assertNotIn("telemetry.sdk.version", warn.data)
        # Trace correlation.
        self.assertEqual(
            str(warn.trace_id).replace("-", ""), "edec519707974fc8bfccb5a017e17394"
        )
        self.assertIsNotNone(warn.span_id)

    def test_gzip_encoded_protobuf_ingested(self):
        """OTLP/HTTP exporters MAY gzip the body (the OpenTelemetry Collector
        does so by default). The body is decompressed in Rust at the view's
        body-read seam, with no decompression middleware in the chain."""
        res = self.client.post(
            self.url,
            gzip.compress(_build_protobuf_request()),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
            HTTP_CONTENT_ENCODING="gzip",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_record_without_body_decodes_empty(self):
        """A record with no body must become "" — not the string "None"."""
        req = ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(
                    scope_logs=[
                        ScopeLogs(
                            log_records=[
                                LogRecord(
                                    time_unix_nano=int(time.time() * 1e9),
                                    severity_number=9,
                                )
                            ]
                        )
                    ]
                )
            ]
        ).SerializeToString()
        items = decode_otlp_logs(req, "application/x-protobuf")
        self.assertEqual(items[0]["body"], "")

    def test_zero_trace_and_span_ids_treated_as_absent(self):
        """All-zero ids mean 'no span context' and must not be stored."""
        req = ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(
                    scope_logs=[
                        ScopeLogs(
                            log_records=[
                                LogRecord(
                                    time_unix_nano=int(time.time() * 1e9),
                                    severity_number=9,
                                    body=AnyValue(string_value="x"),
                                    trace_id=b"\x00" * 16,
                                    span_id=b"\x00" * 8,
                                )
                            ]
                        )
                    ]
                )
            ]
        ).SerializeToString()
        items = decode_otlp_logs(req, "application/x-protobuf")
        self.assertIsNone(items[0]["trace_id"])
        self.assertIsNone(items[0]["span_id"])

    def test_trailing_slash_accepted(self):
        res = self.client.post(
            "/v1/logs/",
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        # Drain the enqueued task so it can't bleed into a later test's flush.
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_json_logs_ingested(self):
        """OTLP/JSON uses camelCase keys and hex trace ids."""
        now_ns = str(int(time.time() * 1e9))
        body = orjson.dumps(
            {
                "resourceLogs": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": "json-svc"},
                                }
                            ]
                        },
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": now_ns,
                                        "severityNumber": 17,
                                        "severityText": "ERROR",
                                        "body": {"stringValue": "Disk full"},
                                        "attributes": [
                                            {
                                                "key": "disk",
                                                "value": {"stringValue": "/dev/sda1"},
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
        res = self.client.post(
            self.url,
            body,
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self._flush()

        self.assertEqual(LogEvent.objects.count(), 1)
        log = LogEvent.objects.first()
        self.assertEqual(log.body, "Disk full")
        self.assertEqual(log.level, LogLevel.ERROR)
        self.assertEqual(log.service, "json-svc")
        self.assertEqual(log.data["disk"], "/dev/sda1")

    def test_auth_via_x_sentry_auth_header(self):
        """OTLP shares auth_from_request, so the sentry X-Sentry-Auth header
        and ?sentry_key query param work too, not just Bearer."""
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_X_SENTRY_AUTH=f"Sentry sentry_key={self.projectkey.public_key.hex}",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_auth_via_query_param(self):
        res = self.client.post(
            f"{self.url}?sentry_key={self.projectkey.public_key.hex}",
            _build_protobuf_request(),
            content_type="application/x-protobuf",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 2)

    def test_multi_record_batch_decodes(self):
        """A 101-record batch (single resource/scope) decodes fully, with the
        resource service/environment hoisted onto every record and SDK
        self-description dropped."""
        items = decode_otlp_logs(_build_batch_request(101), "application/x-protobuf")
        self.assertEqual(len(items), 101)
        self.assertTrue(all(i["service"] == "batch-svc" for i in items))
        self.assertTrue(all(i["environment"] == "prod" for i in items))
        self.assertTrue(all("telemetry.sdk.version" not in i for i in items))
        self.assertIn("log number 0", [i["body"] for i in items])

    def test_invalid_dsn_rejected(self):
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION="Bearer 00000000000000000000000000000000",
        )
        self.assertEqual(res.status_code, 401)
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 0)

    def test_invalid_dsn_block_cached(self):
        """An unknown key is cached as blocked so a repeat flood is bounced
        from Valkey without a DB lookup or reading the request body."""
        bad = "00000000000000000000000000000000"
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=f"Bearer {bad}",
        )
        self.assertEqual(res.status_code, 401)
        block_key = f"{EVENT_BLOCK_CACHE_KEY}otlp:{UUID(bad)}"
        self.assertEqual(cache.get(block_key), "v")
        # Repeat stays rejected (served from cache).
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=f"Bearer {bad}",
        )
        self.assertEqual(res.status_code, 401)

    def test_over_quota_org_throttle_cached(self):
        """An org over quota (100% throttle) is rejected and the throttle is
        cached, so the next request re-blocks from cache instead of hitting the
        DB and reading the body again."""
        self.organization.event_throttle_rate = 100
        self.organization.save()
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.headers.get("Retry-After"), "600")
        block_key = f"{EVENT_BLOCK_CACHE_KEY}otlp:{self.projectkey.public_key}"
        self.assertEqual(cache.get(block_key), "t:100:100")
        # Repeat stays throttled and nothing is ingested.
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 429)
        self._flush()
        self.assertEqual(LogEvent.objects.count(), 0)

    def test_observed_time_used_when_no_timestamp(self):
        """A record carrying only observed time (no original timestamp) must
        use it on both transports, not collapse to the 1970 epoch."""
        observed = int(time.time() * 1e9)
        proto = ExportLogsServiceRequest(
            resource_logs=[
                ResourceLogs(
                    scope_logs=[
                        ScopeLogs(
                            log_records=[
                                LogRecord(
                                    observed_time_unix_nano=observed,
                                    severity_number=9,
                                    body=AnyValue(string_value="x"),
                                )
                            ]
                        )
                    ]
                )
            ]
        ).SerializeToString()
        proto_items = decode_otlp_logs(proto, "application/x-protobuf")
        self.assertAlmostEqual(proto_items[0]["timestamp"], observed / 1e9, places=3)

        json_body = orjson.dumps(
            {
                "resourceLogs": [
                    {
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "observedTimeUnixNano": str(observed),
                                        "severityNumber": 9,
                                        "body": {"stringValue": "x"},
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )
        json_items = decode_otlp_logs(json_body, "application/json")
        self.assertAlmostEqual(json_items[0]["timestamp"], observed / 1e9, places=3)

    def test_missing_auth_rejected(self):
        res = self.client.post(
            self.url,
            _build_protobuf_request(),
            content_type="application/x-protobuf",
        )
        self.assertEqual(res.status_code, 401)

    def test_malformed_payload_rejected(self):
        res = self.client.post(
            self.url,
            b"not a valid protobuf",
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_get_not_allowed(self):
        res = self.client.get(self.url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 405)


class OTLPLogsFeatureFlagTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    @override_settings(GLITCHTIP_ENABLE_LOGS=False)
    def test_feature_flag_off_returns_404(self):
        self.create_project()
        res = self.client.post(
            reverse("otlp_logs"),
            _build_protobuf_request(),
            content_type="application/x-protobuf",
            HTTP_AUTHORIZATION=f"Bearer {self.projectkey.public_key.hex}",
        )
        self.assertEqual(res.status_code, 404)
