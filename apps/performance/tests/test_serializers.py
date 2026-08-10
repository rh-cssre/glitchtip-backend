"""
Tests for performance serialization: MCP serializers and Pydantic schema.

Verifies that computed fields (error_rate, throughput) are consistent
between model properties, Pydantic schema, and MCP serializer output.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from apps.mcp.serializers import serialize_transaction_group
from apps.performance.models import TransactionGroup
from apps.performance.schema import TransactionGroupSchema


class MCPSerializerTestCase(TestCase):
    def _make_group(self, **kwargs):
        now = timezone.now()
        defaults = {
            "project": baker.make("projects.Project"),
            "transaction": "/api/test/",
            "op": "http.server",
            "method": "GET",
            "first_seen": now - timedelta(hours=1),
            "last_seen": now,
            "avg_duration": 100.0,
            "count": 100,
            "error_count": 10,
            "p50": 80.0,
            "p95": 250.0,
        }
        defaults.update(kwargs)
        defaults.setdefault("organization", defaults["project"].organization)
        return TransactionGroup.objects.create(**defaults)

    def test_serialize_includes_all_fields(self):
        group = self._make_group()
        result = serialize_transaction_group(group)

        expected_keys = {
            "id",
            "project",
            "transaction",
            "op",
            "method",
            "count",
            "avgDuration",
            "p50",
            "p95",
            "errorCount",
            "errorRate",
            "throughput",
            "firstSeen",
            "lastSeen",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_error_rate_matches_model(self):
        group = self._make_group(count=200, error_count=50)
        result = serialize_transaction_group(group)
        self.assertEqual(result["errorRate"], group.error_rate)
        self.assertEqual(result["errorRate"], 25.0)

    def test_throughput_matches_model(self):
        group = self._make_group(count=120)
        result = serialize_transaction_group(group)
        self.assertEqual(result["throughput"], group.throughput)
        self.assertIsNotNone(result["throughput"])

    def test_throughput_none_when_zero_span(self):
        now = timezone.now()
        group = self._make_group(first_seen=now, last_seen=now)
        result = serialize_transaction_group(group)
        self.assertIsNone(result["throughput"])

    def test_iso_format_timestamps(self):
        group = self._make_group()
        result = serialize_transaction_group(group)
        self.assertEqual(result["firstSeen"], group.first_seen.isoformat())
        self.assertEqual(result["lastSeen"], group.last_seen.isoformat())


class PydanticSchemaTestCase(TestCase):
    def test_computed_fields_match_model(self):
        """TransactionGroupSchema computed fields delegate to model properties."""
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/api/test/",
            op="http.server",
            method="GET",
            first_seen=now - timedelta(hours=1),
            last_seen=now,
            count=200,
            error_count=30,
            avg_duration=150.0,
        )
        schema = TransactionGroupSchema.from_orm(group)
        data = schema.model_dump(by_alias=True)

        self.assertEqual(data["errorRate"], group.error_rate)
        self.assertEqual(data["throughput"], group.throughput)
        # 30/200 * 100 = 15.0
        self.assertEqual(data["errorRate"], 15.0)

    def test_camel_case_aliases(self):
        """Schema output uses camelCase field names."""
        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
        )
        schema = TransactionGroupSchema.from_orm(group)
        data = schema.model_dump(by_alias=True)

        camel_keys = {"avgDuration", "errorCount", "errorRate", "firstSeen", "lastSeen"}
        for key in camel_keys:
            self.assertIn(key, data, f"Missing camelCase key: {key}")
