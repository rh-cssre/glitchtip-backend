from datetime import datetime, timezone
from uuid import UUID

import requests_mock
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.partition_manager import PartitionManager, UUID7Helper


class SettingsTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:get_settings")

    def test_settings(self):
        with self.assertNumQueries(1):
            res = self.client.get(self.url)  # Check that no auth is necessary
        self.assertEqual(res.status_code, 200)

    def test_settings_oidc(self):
        social_app = baker.make(
            "socialaccount.socialapp",
            provider="openid_connect",
            provider_id="my-openid",
            settings={"server_url": "https://example.com"},
        )
        for provider in [
            "gitlab",
            "microsoft",
            "github",
            "google",
            "nextcloud",
            "digitalocean",
        ]:
            baker.make(
                "socialaccount.socialapp",
                provider=provider,
            )
        with requests_mock.Mocker() as m:
            m.get(
                "https://example.com/.well-known/openid-configuration",
                json={"authorization_endpoint": ""},
            )
            res = self.client.get(self.url)
        self.assertContains(res, social_app.name)


class APIRootTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:api_root")

    def test_anon(self):
        self.assertContains(self.client.get(self.url), "version")

    def test_user(self):
        user = baker.make("users.user")
        self.client.force_login(user)
        res = self.client.get(self.url)
        self.assertContains(res, user.email)

    def test_token(self):
        user = baker.make("users.user")
        auth_token = baker.make("api_tokens.APIToken", user=user)

        headers = {"Authorization": f"Bearer {auth_token.token}"}
        res = self.client.get(self.url, headers=headers)
        self.assertContains(res, auth_token.token)
        self.assertContains(res, user.email)


class APICatchallTestCase(TestCase):
    """Unmatched API paths should return JSON 404, not CSRF error page"""

    def test_unmatched_path_returns_json_404(self):
        res = self.client.get("/api/0/nonexistent/path/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(res.json(), {"detail": "Not found"})

    def test_unmatched_post_returns_json_404(self):
        res = self.client.post("/api/0/nonexistent/path/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res["Content-Type"], "application/json; charset=utf-8")

    def test_unmatched_path_no_trailing_slash(self):
        res = self.client.get("/api/0/nonexistent/path")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res["Content-Type"], "application/json; charset=utf-8")


class InternalHealthTestCase(TestCase):
    def setUp(self):
        self.url = "/api/0/internal/health"

    def test_get_health(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)

        data = res.json()
        self.assertIn("healthy", data)
        self.assertIn("problems", data)


class ObservabilityTestCase(TestCase):
    def test_metrics_endpoint(self):
        from django.urls import include, path

        from glitchtip import urls

        # Manually inject the URL pattern to simulate ENABLE_OBSERVABILITY_API=True
        # This avoids needing to reload the entire URLconf module which is flaky in tests
        pattern = path("", include("django_prometheus.urls"))
        urls.urlpatterns.append(pattern)

        try:
            res = self.client.get("/metrics")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res["Content-Type"].startswith("text/plain"))
        finally:
            urls.urlpatterns.pop()


class UUID7HelperTestCase(TestCase):
    """Test UUID7 timestamp encoding and extraction"""

    def test_from_datetime(self):
        """UUIDv7 encodes timestamp in first 48 bits"""
        dt = datetime(2025, 1, 15, 12, 30, 45, tzinfo=timezone.utc)
        uuid_val = UUID7Helper.from_datetime(dt)

        self.assertEqual(uuid_val.version, 7)
        self.assertIsInstance(uuid_val, UUID)

        # Extract and verify timestamp (within millisecond precision)
        extracted = UUID7Helper.extract_datetime(uuid_val)
        delta = abs((extracted - dt).total_seconds())
        self.assertLess(delta, 0.001)  # Within 1ms

    def test_from_datetime_naive(self):
        """Naive datetime is treated as UTC"""
        dt_naive = datetime(2025, 1, 15, 12, 0, 0)
        dt_aware = dt_naive.replace(tzinfo=timezone.utc)

        uuid_naive = UUID7Helper.from_datetime(dt_naive)
        uuid_aware = UUID7Helper.from_datetime(dt_aware)

        # Timestamps should be identical
        extracted_naive = UUID7Helper.extract_datetime(uuid_naive)
        extracted_aware = UUID7Helper.extract_datetime(uuid_aware)

        delta = abs((extracted_naive - extracted_aware).total_seconds())
        self.assertLess(delta, 0.001)

    def test_extract_datetime(self):
        """Extract datetime from UUIDv7"""
        original_dt = datetime(2025, 6, 15, 9, 15, 30, tzinfo=timezone.utc)
        uuid_val = UUID7Helper.from_datetime(original_dt)

        extracted_dt = UUID7Helper.extract_datetime(uuid_val)

        # Should be timezone-aware UTC
        self.assertIsNotNone(extracted_dt.tzinfo)
        self.assertEqual(extracted_dt.tzinfo, timezone.utc)

        # Timestamps should match within millisecond precision
        delta = abs((extracted_dt - original_dt).total_seconds())
        self.assertLess(delta, 0.001)

    def test_extract_datetime_invalid_version(self):
        """Raises error for non-v7 UUIDs"""
        from uuid import uuid4

        uuid_v4 = uuid4()

        with self.assertRaises(ValueError) as ctx:
            UUID7Helper.extract_datetime(uuid_v4)

        self.assertIn("version 7", str(ctx.exception))

    def test_get_range_for_date(self):
        """UUID range covers full date"""
        start = datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)

        start_uuid, end_uuid = UUID7Helper.get_range_for_date(start, end)

        # Verify both are UUIDv7
        self.assertEqual(start_uuid.version, 7)
        self.assertEqual(end_uuid.version, 7)

        # Verify ordering
        self.assertLess(start_uuid, end_uuid)

        # Verify boundary timestamps
        extracted_start = UUID7Helper.extract_datetime(start_uuid)
        extracted_end = UUID7Helper.extract_datetime(end_uuid)

        self.assertLess(abs((extracted_start - start).total_seconds()), 0.001)
        self.assertLess(abs((extracted_end - end).total_seconds()), 0.001)

    def test_temporal_ordering(self):
        """UUIDv7s maintain temporal ordering"""
        dt1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2025, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        dt3 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        uuid1 = UUID7Helper.from_datetime(dt1)
        uuid2 = UUID7Helper.from_datetime(dt2)
        uuid3 = UUID7Helper.from_datetime(dt3)

        # UUIDs should be sortable by timestamp
        self.assertLess(uuid1, uuid2)
        self.assertLess(uuid2, uuid3)
        self.assertLess(uuid1, uuid3)


class PartitionManagerTestCase(TestCase):
    """Test partition SQL generation"""

    def test_create_time_partition_datetime_mode(self):
        """DateTime mode generates correct SQL"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="issue_events_issueaggregate",
            partition_name="issue_events_issueaggregate_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=4,
            hash_column="organization_id",
            key_type="datetime",
            partition_column="date",
        )

        # Verify parent partition SQL
        parent_sql = sqls[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS", parent_sql)
        self.assertIn("issue_events_issueaggregate_20250115", parent_sql)
        self.assertIn("PARTITION OF issue_events_issueaggregate", parent_sql)
        self.assertIn("FOR VALUES FROM ('2025-01-15", parent_sql)
        self.assertIn("TO ('2025-01-16", parent_sql)
        self.assertIn("PARTITION BY HASH (organization_id)", parent_sql)

        # Verify hash children (4 buckets)
        self.assertEqual(len(sqls), 5)  # 1 parent + 4 children

        # Check first hash partition
        self.assertIn("issue_events_issueaggregate_20250115_h0", sqls[1])
        self.assertIn("MODULUS 4, REMAINDER 0", sqls[1])

        # Check last hash partition
        self.assertIn("issue_events_issueaggregate_20250115_h3", sqls[4])
        self.assertIn("MODULUS 4, REMAINDER 3", sqls[4])

    def test_create_time_partition_uuid7_mode(self):
        """UUID7 mode generates correct SQL with UUID ranges"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="issue_events_issueevent",
            partition_name="issue_events_issueevent_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=16,
            hash_column="organization_id",
            key_type="uuid7",
            partition_column="id",
        )

        parent_sql = sqls[0]

        # Verify UUID range format (should have UUID strings, not datetimes)
        self.assertIn("FOR VALUES FROM ('", parent_sql)
        self.assertIn("TO ('", parent_sql)

        # Should NOT contain datetime strings
        self.assertNotIn("2025-01-15T", parent_sql)

        # Should contain UUID-like strings (hex with dashes)
        # UUIDs are 36 chars: 8-4-4-4-12
        import re

        uuid_pattern = r"'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'"
        matches = re.findall(uuid_pattern, parent_sql, re.IGNORECASE)
        self.assertEqual(len(matches), 2)  # start and end UUID

        # Verify 16 hash children
        self.assertEqual(len(sqls), 17)  # 1 parent + 16 hash children

    def test_idempotent_sql(self):
        """SQL includes IF NOT EXISTS for idempotency"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="test_table",
            partition_name="test_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=2,
        )

        # All statements should be idempotent
        for sql in sqls:
            self.assertIn("IF NOT EXISTS", sql)

    def test_configurable_hash_buckets(self):
        """Hash bucket count is configurable"""
        manager = PartitionManager()

        for bucket_count in [2, 4, 8, 16, 32]:
            sqls = manager.create_time_partition(
                parent_table="test_table",
                partition_name=f"test_b{bucket_count}",
                start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
                end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
                hash_buckets=bucket_count,
            )

            # Should have 1 parent + N hash children
            self.assertEqual(len(sqls), bucket_count + 1)

            # Verify MODULUS matches bucket count
            for i in range(bucket_count):
                self.assertIn(f"MODULUS {bucket_count}", sqls[i + 1])
                self.assertIn(f"REMAINDER {i}", sqls[i + 1])

    def test_configurable_hash_column(self):
        """Hash column is configurable"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="test_table",
            partition_name="test_custom_hash",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=4,
            hash_column="project_id",
        )

        # Parent should partition by custom column
        self.assertIn("PARTITION BY HASH (project_id)", sqls[0])

    def test_drop_partition(self):
        """Generate DROP TABLE SQL"""
        manager = PartitionManager()

        sql = manager.drop_partition("test_partition_20250115")

        self.assertIn("DROP TABLE IF EXISTS", sql)
        self.assertIn("test_partition_20250115", sql)
        self.assertIn("CASCADE", sql)

    def test_datetime_timezone_handling(self):
        """Naive datetimes are treated as UTC"""
        manager = PartitionManager()

        # Test with naive datetime
        sqls_naive = manager.create_time_partition(
            parent_table="test_table",
            partition_name="test_naive",
            start_date=datetime(2025, 1, 15, 0, 0, 0),
            end_date=datetime(2025, 1, 16, 0, 0, 0),
            hash_buckets=2,
            key_type="datetime",
        )

        # Test with aware datetime
        sqls_aware = manager.create_time_partition(
            parent_table="test_table",
            partition_name="test_aware",
            start_date=datetime(2025, 1, 15, 0, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc),
            hash_buckets=2,
            key_type="datetime",
        )

        # Both should produce valid SQL with timezone info
        self.assertIn("2025-01-15", sqls_naive[0])
        self.assertIn("2025-01-15", sqls_aware[0])

    def test_partition_naming_convention(self):
        """Partition names follow expected convention"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="issue_events_issueaggregate",
            partition_name="issue_events_issueaggregate_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=4,
        )

        # Parent partition
        self.assertIn("issue_events_issueaggregate_20250115", sqls[0])

        # Hash children: parent_name + _hN
        for i in range(4):
            expected_name = f"issue_events_issueaggregate_20250115_h{i}"
            self.assertIn(expected_name, sqls[i + 1])


class DatabaseSettingsTestCase(TestCase):
    def test_database_settings_defaults(self):
        """
        Verify that database settings are applied correctly.
        Note: In TESTING mode, some values are overridden (see settings.py).
        """
        from django.conf import settings

        db_settings = settings.DATABASES["default"]
        # In TESTING mode, CONN_MAX_AGE is explicitly set to None
        self.assertIsNone(db_settings.get("CONN_MAX_AGE"))
        self.assertEqual(db_settings.get("CONN_HEALTH_CHECKS"), False)
        self.assertEqual(db_settings.get("DISABLE_SERVER_SIDE_CURSORS"), True)
        # In TESTING mode, pool is explicitly set to False
        self.assertEqual(db_settings.get("OPTIONS", {}).get("pool"), False)
        self.assertEqual(db_settings.get("ENGINE"), "django.db.backends.postgresql")
