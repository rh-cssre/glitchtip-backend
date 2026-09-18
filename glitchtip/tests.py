import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

from django.conf import settings
from django.core import signals
from django.db import connection
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django_async_backend.db import async_connections, close_old_async_connections
from django_vtasks.signals import task_failure, task_finished
from model_bakery import baker

from apps.stripe.models import SupportLicense
from glitchtip.async_db import (
    MIDDLEWARE_PATH,
    check_async_connection_recycling,
    recycle_async_connections,
    recycle_async_connections_middleware,
)
from glitchtip.ingest_asgi import INGEST_MIDDLEWARE
from glitchtip.internal_transport import InternalTransport, _processing_internal
from glitchtip.partition_manager import PartitionManager, UUID7Helper
from glitchtip.settings import _is_self_referencing_dsn


class SettingsTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:get_settings")

    def test_settings(self):
        with self.assertNumQueries(1):
            res = self.client.get(self.url)  # Check that no auth is necessary
        self.assertEqual(res.status_code, 200)

    def test_settings_does_not_expose_license_key(self):
        res = self.client.get(self.url)
        self.assertNotIn("licenseKey", res.json())
        self.assertNotIn("license_key", res.json())

    @override_settings(BILLING_ENABLED=False, I_PAID_FOR_GLITCHTIP=False)
    def test_i_paid_for_glitchtip_reflects_support_license(self):
        res = self.client.get(self.url)
        self.assertFalse(res.json()["iPaidForGlitchTip"])
        SupportLicense(license_key="sub_xxx").save()
        res = self.client.get(self.url)
        self.assertTrue(res.json()["iPaidForGlitchTip"])

    @override_settings(BILLING_ENABLED=False, I_PAID_FOR_GLITCHTIP=True)
    def test_legacy_i_paid_env_var_still_overrides(self):
        res = self.client.get(self.url)
        self.assertTrue(res.json()["iPaidForGlitchTip"])

    async def test_settings_oidc(self):
        social_app = await baker.amake(
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
            await baker.amake(
                "socialaccount.socialapp",
                provider=provider,
            )
        # Serializing an app must never reach the network: this endpoint is
        # unauthenticated and lists every app, so an outbound call per app
        # would let one anonymous request fan out across the IdPs they name.
        with patch("aiohttp.ClientSession.get", side_effect=AssertionError) as no_http:
            res = await self.async_client.get(self.url)
        no_http.assert_not_called()
        self.assertContains(res, social_app.name)
        # OIDC apps report provider_id, which is what the headless redirect
        # needs to tell several apps of the same brand apart.
        self.assertContains(res, "my-openid")


class InstanceLicenseTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:get_instance_license")
        self.user = baker.make("users.user")

    def test_requires_auth(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 401)

    @override_settings(BILLING_ENABLED=False)
    def test_empty_when_unconfigured(self):
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"billingEmail": ""})

    @override_settings(BILLING_ENABLED=False)
    def test_returns_db_billing_email(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.json(), {"billingEmail": "db@example.com"})

    @override_settings(BILLING_ENABLED=True)
    def test_billing_enabled_returns_empty(self):
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.json(), {"billingEmail": ""})


class SupportLinkTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:get_support_link")
        self.user = baker.make("users.user")

    def test_requires_auth(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 401)

    @override_settings(BILLING_ENABLED=False)
    def test_returns_base_url_when_unlicensed(self):
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.json(), {"url": "https://glitchtip.com/support"})

    @override_settings(BILLING_ENABLED=False)
    def test_embeds_license_key_only(self):
        SupportLicense(license_key="sub_dbKey").save()
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(
            res.json(), {"url": "https://glitchtip.com/support#sub=sub_dbKey"}
        )

    @override_settings(BILLING_ENABLED=True)
    def test_billing_enabled_returns_base_url(self):
        SupportLicense(license_key="sub_dbKey").save()
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.json(), {"url": "https://glitchtip.com/support"})


class APIRootTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:api_root")

    def test_anon(self):
        self.assertContains(self.client.get(self.url), "version")

    async def test_user(self):
        user = await baker.amake("users.user")
        await self.async_client.aforce_login(user)
        res = await self.async_client.get(self.url)
        self.assertContains(res, user.email)

    async def test_token(self):
        user = await baker.amake("users.user")
        auth_token = await baker.amake("api_tokens.APIToken", user=user)

        headers = {"Authorization": f"Bearer {auth_token.token}"}
        res = await self.async_client.get(self.url, headers=headers)
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
        self.url = "/api/0/internal/health/"

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
        dt = datetime(2025, 1, 15, 12, 30, 45, tzinfo=UTC)
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
        dt_aware = dt_naive.replace(tzinfo=UTC)

        uuid_naive = UUID7Helper.from_datetime(dt_naive)
        uuid_aware = UUID7Helper.from_datetime(dt_aware)

        # Timestamps should be identical
        extracted_naive = UUID7Helper.extract_datetime(uuid_naive)
        extracted_aware = UUID7Helper.extract_datetime(uuid_aware)

        delta = abs((extracted_naive - extracted_aware).total_seconds())
        self.assertLess(delta, 0.001)

    def test_extract_datetime(self):
        """Extract datetime from UUIDv7"""
        original_dt = datetime(2025, 6, 15, 9, 15, 30, tzinfo=UTC)
        uuid_val = UUID7Helper.from_datetime(original_dt)

        extracted_dt = UUID7Helper.extract_datetime(uuid_val)

        # Should be timezone-aware UTC
        self.assertIsNotNone(extracted_dt.tzinfo)
        self.assertEqual(extracted_dt.tzinfo, UTC)

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
        start = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        end = datetime(2025, 1, 16, 0, 0, 0, tzinfo=UTC)

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
        dt1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        dt2 = datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC)
        dt3 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)

        uuid1 = UUID7Helper.from_datetime(dt1)
        uuid2 = UUID7Helper.from_datetime(dt2)
        uuid3 = UUID7Helper.from_datetime(dt3)

        # UUIDs should be sortable by timestamp
        self.assertLess(uuid1, uuid2)
        self.assertLess(uuid2, uuid3)
        self.assertLess(uuid1, uuid3)

    def test_pre_epoch_date_clamped(self):
        """Dates before Unix epoch produce valid UUIDs (clamped to epoch)"""
        pre_epoch = datetime.min.replace(tzinfo=UTC)
        start_uuid, end_uuid = UUID7Helper.get_range_for_date(
            pre_epoch, datetime(2025, 1, 1, tzinfo=UTC)
        )
        self.assertEqual(start_uuid.version, 7)
        self.assertEqual(end_uuid.version, 7)
        self.assertLess(start_uuid, end_uuid)


class PartitionManagerTestCase(TestCase):
    """Test partition SQL generation"""

    def test_create_time_partition_datetime_mode(self):
        """DateTime mode generates correct SQL"""
        manager = PartitionManager()

        sqls = manager.create_time_partition(
            parent_table="issue_events_issueaggregate",
            partition_name="issue_events_issueaggregate_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, tzinfo=UTC),
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
            start_date=datetime(2025, 1, 15, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, tzinfo=UTC),
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
            start_date=datetime(2025, 1, 15, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, tzinfo=UTC),
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
                start_date=datetime(2025, 1, 15, tzinfo=UTC),
                end_date=datetime(2025, 1, 16, tzinfo=UTC),
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
            start_date=datetime(2025, 1, 15, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, tzinfo=UTC),
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
            start_date=datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, 0, 0, 0, tzinfo=UTC),
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
            start_date=datetime(2025, 1, 15, tzinfo=UTC),
            end_date=datetime(2025, 1, 16, tzinfo=UTC),
            hash_buckets=4,
        )

        # Parent partition
        self.assertIn("issue_events_issueaggregate_20250115", sqls[0])

        # Hash children: parent_name + _hN
        for i in range(4):
            expected_name = f"issue_events_issueaggregate_20250115_h{i}"
            self.assertIn(expected_name, sqls[i + 1])


class PartitionCreationDDLTestCase(TransactionTestCase):
    """
    Exercise execute_partition_creation against a real partitioned table.

    These are catalog-level operations (CREATE TABLE ... PARTITION OF, then
    reading pg_inherits/pg_class), so they need a real Postgres backend rather
    than mocked SQL generation.
    """

    # Disposable parent table created/dropped per test.
    PARENT = "test_partition_ddl_parent"

    def setUp(self):
        super().setUp()
        self.manager = PartitionManager()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.PARENT} CASCADE;")
            # RANGE on a date column, sub-partitioned by HASH on organization_id
            # to mirror the events tables (TIME -> HASH).
            cursor.execute(
                f"""
                CREATE TABLE {self.PARENT} (
                    organization_id bigint NOT NULL,
                    date date NOT NULL
                ) PARTITION BY RANGE (date);
                """
            )

    def tearDown(self):
        # Discard any aborted-transaction state from a failed assertion so the
        # cleanup DROP runs on a healthy connection and never masks the failure.
        connection.close()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.PARENT} CASCADE;")
        super().tearDown()

    @contextmanager
    def _assert_partition_logs(self, level):
        """assertLogs on the partition manager that survives the settings-level
        ``logging.disable(logging.WARNING)`` applied during tests (see
        ``glitchtip/settings.py``). That global disable suppresses INFO and
        WARNING records regardless of ``assertLogs``'s own handler, so lift it
        for the duration of the assertion and restore it afterwards.
        """
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("glitchtip.partition_manager", level=level) as logs:
                yield logs
        finally:
            logging.disable(previous)

    def _child_moduli(self, partition_name):
        """Return the sorted set of hash moduli among a partition's children."""
        moduli = set()
        for child in self.manager.list_partitions(partition_name):
            match = re.search(
                r"modulus\s+(\d+)", child.get("partition_bounds") or "", re.IGNORECASE
            )
            if match:
                moduli.add(int(match.group(1)))
        return sorted(moduli)

    def test_skips_when_existing_modulus_differs(self):
        """
        A range partition pre-created at modulus 4 must NOT gain modulus-16
        children when the configured bucket count is raised to 16 — that would
        raise 'partition ... would overlap partition ...'. The existing mod-4
        set is already complete and must be left intact.
        """
        partition_name = f"{self.PARENT}_20260629"
        start = datetime(2026, 6, 29, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)

        # Pre-create the range partition with 4 hash buckets (old bucket count).
        created = self.manager.execute_partition_creation(
            parent_table=self.PARENT,
            partition_name=partition_name,
            start_date=start,
            end_date=end,
            hash_buckets=4,
            key_type="datetime",
        )
        self.assertEqual(created, 5)  # 1 range + 4 hash children
        self.assertEqual(self._child_moduli(partition_name), [4])

        # Now run again with the raised bucket count. This must be a no-op.
        with self._assert_partition_logs("INFO") as logs:
            result = self.manager.execute_partition_creation(
                parent_table=self.PARENT,
                partition_name=partition_name,
                start_date=start,
                end_date=end,
                hash_buckets=16,
                key_type="datetime",
            )

        self.assertEqual(result, 0)
        # The existing mod-4 set is untouched (no mod-16 children created).
        self.assertEqual(self._child_moduli(partition_name), [4])
        self.assertEqual(len(self.manager.list_partitions(partition_name)), 4)
        self.assertTrue(
            any("leaving as-is" in message for message in logs.output),
            logs.output,
        )

    def test_fills_incomplete_legacy_set_at_its_own_modulus(self):
        """
        A partition left with an INCOMPLETE hash set at an older modulus must be
        repaired at that modulus, even though it differs from the configured
        bucket count. The missing remainder tiles none of the keyspace, so rows
        hashing there are rejected with 23514 — and since lowering the
        configured count makes every legacy partition take the differing-modulus
        path, bailing out here would leave a slice of organizations unable to
        ingest for the rest of the partition's retention window.
        """
        partition_name = f"{self.PARENT}_20260703"
        start = datetime(2026, 7, 3, tzinfo=UTC)
        end = datetime(2026, 7, 4, tzinfo=UTC)

        # A legacy modulus-4 set that is missing remainder 3.
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE {partition_name} PARTITION OF {self.PARENT}
                FOR VALUES FROM ('2026-07-03') TO ('2026-07-04')
                PARTITION BY HASH (organization_id);
                """
            )
            for remainder in range(3):
                cursor.execute(
                    f"""
                    CREATE TABLE {partition_name}_h{remainder}
                    PARTITION OF {partition_name}
                    FOR VALUES WITH (MODULUS 4, REMAINDER {remainder});
                    """
                )

        # Configured count is 2 — it cannot be applied to a modulus-4 set, but
        # the gap still has to be filled.
        with self._assert_partition_logs("WARNING") as logs:
            result = self.manager.execute_partition_creation(
                parent_table=self.PARENT,
                partition_name=partition_name,
                start_date=start,
                end_date=end,
                hash_buckets=2,
                key_type="datetime",
            )

        self.assertEqual(result, 1)
        self.assertEqual(len(self.manager.list_partitions(partition_name)), 4)
        # Repaired at the set's own modulus, not the configured one.
        self.assertEqual(self._child_moduli(partition_name), [4])
        self.assertTrue(
            any("incomplete" in message for message in logs.output), logs.output
        )

        # Every organization can now be routed. Without the missing remainder
        # some of these rows raise 23514 "no partition of relation found".
        with connection.cursor() as cursor:
            for organization_id in range(1, 40):
                cursor.execute(
                    f"INSERT INTO {self.PARENT} (date, organization_id) "
                    f"VALUES ('2026-07-03', %s);",
                    [organization_id],
                )

    def test_skips_plain_leaf_range_partition(self):
        """
        A range partition created as a plain leaf (hash_buckets=0, as the legacy
        event importer does) must be left alone rather than have hash children
        forced onto it — that raises 42P17 and would abort the whole maintenance
        run, starving later tables of their partitions.
        """
        partition_name = f"{self.PARENT}_20260704"
        start = datetime(2026, 7, 4, tzinfo=UTC)
        end = datetime(2026, 7, 5, tzinfo=UTC)

        self.manager.execute_partition_creation(
            parent_table=self.PARENT,
            partition_name=partition_name,
            start_date=start,
            end_date=end,
            hash_buckets=0,
            key_type="datetime",
        )
        self.assertFalse(self.manager.is_table_partitioned(partition_name))

        with self._assert_partition_logs("WARNING") as logs:
            result = self.manager.execute_partition_creation(
                parent_table=self.PARENT,
                partition_name=partition_name,
                start_date=start,
                end_date=end,
                hash_buckets=2,
                key_type="datetime",
            )

        self.assertEqual(result, 0)
        self.assertEqual(self.manager.list_partitions(partition_name), [])
        self.assertTrue(
            any("plain leaf" in message for message in logs.output), logs.output
        )

    def test_fills_genuinely_missing_bucket_at_matching_modulus(self):
        """
        The healthy gap-fill path still works: if a range partition has hash
        children at the configured modulus but one bucket is missing, the
        missing bucket is created.
        """
        partition_name = f"{self.PARENT}_20260630"
        start = datetime(2026, 6, 30, tzinfo=UTC)
        end = datetime(2026, 7, 1, tzinfo=UTC)

        # Create the range partition plus 3 of the 4 hash children, leaving
        # remainder 3 missing.
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE {partition_name} PARTITION OF {self.PARENT}
                FOR VALUES FROM ('2026-06-30') TO ('2026-07-01')
                PARTITION BY HASH (organization_id);
                """
            )
            for remainder in range(3):
                cursor.execute(
                    f"""
                    CREATE TABLE {partition_name}_h{remainder}
                    PARTITION OF {partition_name}
                    FOR VALUES WITH (MODULUS 4, REMAINDER {remainder});
                    """
                )

        self.assertEqual(len(self.manager.list_partitions(partition_name)), 3)

        # Gap-fill at the matching modulus should create exactly the one
        # missing bucket.
        result = self.manager.execute_partition_creation(
            parent_table=self.PARENT,
            partition_name=partition_name,
            start_date=start,
            end_date=end,
            hash_buckets=4,
            key_type="datetime",
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(self.manager.list_partitions(partition_name)), 4)
        self.assertEqual(self._child_moduli(partition_name), [4])

    def test_matching_modulus_complete_set_is_noop(self):
        """A complete set at the configured modulus creates nothing."""
        partition_name = f"{self.PARENT}_20260701"
        start = datetime(2026, 7, 1, tzinfo=UTC)
        end = datetime(2026, 7, 2, tzinfo=UTC)

        self.manager.execute_partition_creation(
            parent_table=self.PARENT,
            partition_name=partition_name,
            start_date=start,
            end_date=end,
            hash_buckets=4,
            key_type="datetime",
        )

        result = self.manager.execute_partition_creation(
            parent_table=self.PARENT,
            partition_name=partition_name,
            start_date=start,
            end_date=end,
            hash_buckets=4,
            key_type="datetime",
        )
        self.assertEqual(result, 0)
        self.assertEqual(self._child_moduli(partition_name), [4])

    def test_skips_when_children_have_no_hash_modulus(self):
        """
        A range partition whose existing child is NOT a hash partition (e.g.
        sub-partitioned by RANGE/LIST, or any unexpected structure) must be
        left untouched rather than have hash children forced onto it.
        """
        partition_name = f"{self.PARENT}_20260702"
        start = datetime(2026, 7, 2, tzinfo=UTC)
        end = datetime(2026, 7, 3, tzinfo=UTC)

        # Build a range partition that is itself sub-partitioned by LIST, with
        # one list child — so its children carry no hash "modulus" bound.
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE {partition_name} PARTITION OF {self.PARENT}
                FOR VALUES FROM ('2026-07-02') TO ('2026-07-03')
                PARTITION BY LIST (organization_id);
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE {partition_name}_org1 PARTITION OF {partition_name}
                FOR VALUES IN (1);
                """
            )

        with self._assert_partition_logs("WARNING") as logs:
            result = self.manager.execute_partition_creation(
                parent_table=self.PARENT,
                partition_name=partition_name,
                start_date=start,
                end_date=end,
                hash_buckets=4,
                key_type="datetime",
            )

        self.assertEqual(result, 0)
        # No hash children were created; the single list child is untouched.
        self.assertEqual(self._child_moduli(partition_name), [])
        self.assertEqual(len(self.manager.list_partitions(partition_name)), 1)
        self.assertTrue(
            any("leaving as-is" in message for message in logs.output),
            logs.output,
        )


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
        # Guard against a typo or accidental SQLite fallback. The Rust
        # driver is the only supported database engine.
        self.assertEqual(db_settings.get("ENGINE"), "gt_rust.django_backend")


class IsSelfReferencingDsnTestCase(TestCase):
    """Test _is_self_referencing_dsn detection."""

    def _url(self, url_str):
        """Create a parsed URL result similar to env.url()."""
        from urllib.parse import urlparse

        return urlparse(url_str)

    def test_same_host_same_port(self):
        self.assertTrue(
            _is_self_referencing_dsn(
                "http://key@localhost:8000/1", self._url("http://localhost:8000")
            )
        )

    def test_same_host_default_https_port(self):
        self.assertTrue(
            _is_self_referencing_dsn(
                "https://key@example.com/1", self._url("https://example.com")
            )
        )

    def test_same_host_default_http_port(self):
        self.assertTrue(
            _is_self_referencing_dsn(
                "http://key@example.com/1", self._url("http://example.com")
            )
        )

    def test_different_host(self):
        self.assertFalse(
            _is_self_referencing_dsn(
                "https://key@sentry.io/1", self._url("https://glitchtip.example.com")
            )
        )

    def test_same_host_different_port(self):
        self.assertFalse(
            _is_self_referencing_dsn(
                "http://key@localhost:9000/1", self._url("http://localhost:8000")
            )
        )

    def test_docker_hostname(self):
        self.assertTrue(
            _is_self_referencing_dsn(
                "http://key@web:8000/1", self._url("http://web:8000")
            )
        )


class InternalTransportTestCase(TestCase):
    """Test InternalTransport enqueues events correctly."""

    def setUp(self):
        self.organization = baker.make(
            "organizations_ext.Organization", slug="test-internal"
        )
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project_key = baker.make("projects.ProjectKey", project=self.project)

    def _make_envelope(self, item_type, payload):
        """Helper to create a real SDK Envelope with a single item."""
        from sentry_sdk.envelope import Envelope, Item, PayloadRef

        envelope = Envelope()
        envelope.add_item(Item(type=item_type, payload=PayloadRef(json=payload)))
        return envelope

    def test_capture_envelope_enqueues_event(self):
        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})

        envelope = self._make_envelope(
            "event",
            {
                "event_id": "abcd1234abcd1234abcd1234abcd1234",
                "exception": {"values": [{"type": "ValueError", "value": "test"}]},
                "level": "error",
                "platform": "python",
            },
        )

        with patch("apps.event_ingest.tasks.ingest_event") as mock_ingest:
            transport.capture_envelope(envelope)

        mock_ingest.enqueue.assert_called_once()
        args = mock_ingest.enqueue.call_args[0][0]
        self.assertEqual(args["project_id"], self.project.id)
        self.assertEqual(args["organization_id"], self.organization.id)

    def test_capture_envelope_empty_is_noop(self):
        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})

        from sentry_sdk.envelope import Envelope

        envelope = Envelope()

        with patch("apps.event_ingest.tasks.ingest_event") as mock_ingest:
            transport.capture_envelope(envelope)

        mock_ingest.enqueue.assert_not_called()

    def test_capture_envelope_bad_dsn_is_noop(self):
        transport = InternalTransport(options={"dsn": "http://0000@localhost:8000/999"})

        envelope = self._make_envelope("event", {"exception": {}})

        with patch("apps.event_ingest.tasks.ingest_event") as mock_ingest:
            transport.capture_envelope(envelope)

        mock_ingest.enqueue.assert_not_called()

    def test_capture_envelope_enqueues_transaction(self):
        """Verify InternalTransport forwards transaction envelope items."""
        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})

        envelope = self._make_envelope(
            "transaction",
            {
                "event_id": "bbbb1234bbbb1234bbbb1234bbbb1234",
                "type": "transaction",
                "transaction": "/api/test",
                "contexts": {"trace": {"op": "http.server", "trace_id": "a" * 32}},
                "start_timestamp": "2026-01-01T00:00:00Z",
                "timestamp": "2026-01-01T00:00:01Z",
                "spans": [],
            },
        )

        with (
            patch("apps.event_ingest.tasks.ingest_event") as mock_event,
            patch("apps.event_ingest.tasks.ingest_transaction") as mock_txn,
        ):
            transport.capture_envelope(envelope)

        mock_event.enqueue.assert_not_called()
        mock_txn.enqueue.assert_called_once()
        args = mock_txn.enqueue.call_args[0][0]
        self.assertEqual(args["project_id"], self.project.id)
        self.assertEqual(args["organization_id"], self.organization.id)
        self.assertEqual(args["payload"]["transaction"], "/api/test")

    @override_settings(GLITCHTIP_ENABLE_LOGS=True)
    def test_capture_envelope_enqueues_logs(self):
        """Verify InternalTransport forwards log envelope items."""
        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})

        from sentry_sdk.envelope import Envelope, Item, PayloadRef

        envelope = Envelope()
        envelope.add_item(
            Item(
                type="log",
                content_type="application/vnd.sentry.items.log+json",
                headers={"item_count": 1},
                payload=PayloadRef(
                    json={
                        "items": [
                            {
                                "timestamp": 1700000000.0,
                                "trace_id": "00000000-0000-0000-0000-000000000000",
                                "level": "info",
                                "body": "hello from internal transport",
                                "attributes": {
                                    "sentry.severity_number": {
                                        "value": 9,
                                        "type": "integer",
                                    },
                                    "sentry.severity_text": {
                                        "value": "info",
                                        "type": "string",
                                    },
                                },
                            }
                        ]
                    }
                ),
            )
        )

        with patch("apps.logs.tasks.ingest_logs") as mock_ingest:
            transport.capture_envelope(envelope)

        mock_ingest.enqueue.assert_called_once()
        args = mock_ingest.enqueue.call_args[0][0]
        self.assertEqual(args["project_id"], self.project.id)
        self.assertEqual(args["organization_id"], self.organization.id)
        self.assertEqual(len(args["logs"]), 1)
        self.assertEqual(args["logs"][0]["body"], "hello from internal transport")

    def test_sets_processing_internal_contextvar(self):
        """Verify _processing_internal is set during envelope processing."""
        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})

        observed_values = []

        original_process = transport._process_envelope

        def spy_process(envelope):
            observed_values.append(_processing_internal.get())
            original_process(envelope)

        envelope = self._make_envelope(
            "event",
            {
                "event_id": "abcd1234abcd1234abcd1234abcd1234",
                "exception": {"values": []},
            },
        )

        with (
            patch.object(transport, "_process_envelope", spy_process),
            patch("apps.event_ingest.tasks.ingest_event"),
        ):
            transport.capture_envelope(envelope)

        self.assertEqual(observed_values, [True])
        # After return, contextvar should be reset
        self.assertFalse(_processing_internal.get())

    def test_capture_envelope_works_in_async_context(self):
        """Verify capture_envelope dispatches to thread pool in async context."""
        import asyncio

        transport = InternalTransport(options={"dsn": self.project_key.get_dsn()})
        # Pre-cache project_key so the thread pool doesn't need DB access
        # (separate thread can't see test transaction's uncommitted data)
        transport._project_key = self.project_key

        envelope = self._make_envelope(
            "event",
            {
                "event_id": "abcd1234abcd1234abcd1234abcd1234",
                "exception": {"values": [{"type": "ValueError", "value": "async"}]},
            },
        )

        async def run():
            with patch("apps.event_ingest.tasks.ingest_event") as mock_ingest:
                transport.capture_envelope(envelope)
                # Give the background task time to complete in thread pool
                await asyncio.sleep(0.5)
            return mock_ingest

        mock_ingest = asyncio.run(run())
        mock_ingest.enqueue.assert_called_once()
        args = mock_ingest.enqueue.call_args[0][0]
        self.assertEqual(args["project_id"], self.project.id)


class BeforeSendSelfRefTestCase(TestCase):
    """Test before_send behavior with self-referencing guards."""

    def _make_before_send(self, is_self_ref):
        """Create a before_send function with controlled self-ref flag."""
        from django.http import UnreadablePostError

        _UNSAFE_MODULES = ("apps.event_ingest", "apps.logs.process")

        def before_send(event, hint):
            if (
                "log_record" in hint
                and hint["log_record"].name == "django.security.DisallowedHost"
            ):
                return None
            if "exc_info" in hint:
                _, exc_value, _ = hint["exc_info"]
                if isinstance(exc_value, UnreadablePostError):
                    return None
            if is_self_ref:
                if _processing_internal.get():
                    return None
                if "log_record" in hint and hint["log_record"].name.startswith(
                    ("apps.event_ingest", "apps.logs")
                ):
                    return None
                for exc_val in event.get("exception", {}).get("values", []):
                    for frame in exc_val.get("stacktrace", {}).get("frames", []):
                        if frame.get("module", "").startswith(_UNSAFE_MODULES):
                            return None
            return event

        return before_send

    def test_self_ref_drops_during_processing(self):
        before_send = self._make_before_send(True)
        token = _processing_internal.set(True)
        try:
            result = before_send({"exception": {}}, {})
            self.assertIsNone(result)
        finally:
            _processing_internal.reset(token)

    def test_self_ref_drops_ingest_log_record(self):
        before_send = self._make_before_send(True)
        record = logging.LogRecord(
            "apps.event_ingest.views", logging.ERROR, "", 0, "msg", (), None
        )
        result = before_send({"exception": {}}, {"log_record": record})
        self.assertIsNone(result)

    def test_self_ref_drops_ingest_stackframe(self):
        before_send = self._make_before_send(True)
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"module": "apps.event_ingest.process_event"},
                            ]
                        }
                    }
                ]
            }
        }
        result = before_send(event, {})
        self.assertIsNone(result)

    def test_non_self_ref_passes_ingest_stackframe(self):
        before_send = self._make_before_send(False)
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"module": "apps.event_ingest.process_event"},
                            ]
                        }
                    }
                ]
            }
        }
        result = before_send(event, {})
        self.assertIsNotNone(result)

    def test_self_ref_passes_normal_error(self):
        before_send = self._make_before_send(True)
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"module": "apps.users.views"},
                            ]
                        }
                    }
                ]
            }
        }
        result = before_send(event, {})
        self.assertIsNotNone(result)


class ColdStorageFreeTierGatingTestCase(TestCase):
    """Test that free-tier orgs are excluded from cold storage when billing is enabled."""

    def setUp(self):
        self.org_free = baker.make("organizations_ext.Organization", slug="free-org")
        subscription = baker.make("stripe.StripeSubscription")
        self.org_paid = baker.make(
            "organizations_ext.Organization",
            slug="paid-org",
            stripe_primary_subscription=subscription,
        )

    def _get_eligible_org_ids(self, org_ids):
        """Simulate the filtering logic from archive_partition_per_org."""
        from apps.organizations_ext.models import Organization

        if settings.BILLING_ENABLED:
            eligible_ids = set(
                Organization.objects.filter(
                    id__in=org_ids,
                    stripe_primary_subscription__isnull=False,
                ).values_list("id", flat=True)
            )
            return [oid for oid in org_ids if oid in eligible_ids]
        return org_ids

    @override_settings(BILLING_ENABLED=False)
    def test_billing_disabled_returns_all(self):
        """Without billing, all orgs are eligible for cold storage."""
        org_ids = [self.org_free.id, self.org_paid.id]
        result = self._get_eligible_org_ids(org_ids)
        self.assertEqual(result, org_ids)

    @override_settings(BILLING_ENABLED=True)
    def test_billing_enabled_excludes_free_tier(self):
        """With billing, only orgs with a subscription get cold storage."""
        org_ids = [self.org_free.id, self.org_paid.id]
        result = self._get_eligible_org_ids(org_ids)
        self.assertEqual(result, [self.org_paid.id])

    @override_settings(BILLING_ENABLED=True)
    def test_billing_enabled_all_free(self):
        """With billing, if all orgs are free tier, result is empty."""
        org_ids = [self.org_free.id]
        result = self._get_eligible_org_ids(org_ids)
        self.assertEqual(result, [])


# Helper script executed in a subprocess to inspect Django settings under
# controlled env vars.  Prints a JSON dict of the settings we care about.
_SETTINGS_PROBE = """\
import django, os, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()
from django.conf import settings
json.dump({
    "cache_backend": settings.CACHES["default"]["BACKEND"],
    "cache_location": settings.CACHES["default"].get("LOCATION", ""),
    "cache_options": settings.CACHES["default"].get("OPTIONS", {}),
    "task_backend": settings.TASKS["default"]["BACKEND"],
    "session_engine": settings.SESSION_ENGINE,
    "database_engine": settings.DATABASE_ENGINE,
    # "absent" distinguishes "settings.py never set it" from an explicit None.
    "prepare_threshold": settings.DATABASES["default"]["OPTIONS"].get(
        "prepare_threshold", "absent"
    ),
    "iam_provider": settings.DATABASES["default"]["OPTIONS"].get(
        "credential_provider", "absent"
    ),
    "sslmode": settings.DATABASES["default"]["OPTIONS"].get("sslmode", "absent"),
    "password": settings.DATABASES["default"].get("PASSWORD", "absent"),
}, __import__("sys").stdout)
"""


class CacheConfigTestCase(TestCase):
    """
    Boot Django in a subprocess with different env vars and verify that
    CACHES, TASKS, and SESSION_ENGINE are wired correctly.
    """

    # Base env: just enough to let settings.py load (needs DATABASE_URL at minimum).
    # Does NOT include any VALKEY_*/REDIS_* vars — each test sets exactly what it needs.
    # We pass env={} to subprocess so the parent process env is NOT inherited.
    BASE_ENV = {
        "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
        "DATABASE_URL": settings.DATABASES["default"]["NAME"]
        and f"postgres://postgres:postgres@localhost/{settings.DATABASES['default']['NAME']}"
        or "postgres://postgres:postgres@localhost/glitchtip",
        "SECRET_KEY": "test-secret-key",
    }

    VCACHE_BACKEND = "django_vcache.backend.ValkeyCache"
    VTASKS_VALKEY = "django_vtasks.backends.valkey.ValkeyTaskBackend"
    DB_CACHE_BACKEND = "django.core.cache.backends.db.DatabaseCache"
    VTASKS_DB = "django_vtasks.backends.db.DatabaseTaskBackend"

    def _probe(self, extra_env: dict) -> dict:
        """Run the probe script with BASE_ENV + extra_env, return parsed JSON."""
        env = {**self.BASE_ENV, **extra_env}
        result = subprocess.run(
            [sys.executable, "-c", _SETTINGS_PROBE],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Settings probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )
        # stdout may contain the startup banner before the JSON — take last line
        lines = result.stdout.strip().splitlines()
        return json.loads(lines[-1])

    def test_valkey_url_single_node(self):
        """VALKEY_URL=redis://host:6379/0 → vcache + valkey tasks."""
        info = self._probe({"VALKEY_URL": "redis://valkey:6379/0"})
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "redis://valkey:6379/0")
        self.assertEqual(info["task_backend"], self.VTASKS_VALKEY)
        self.assertEqual(
            info["session_engine"], "django.contrib.sessions.backends.cache"
        )

    def test_redis_url_fallback(self):
        """REDIS_URL works as a fallback for VALKEY_URL."""
        info = self._probe({"REDIS_URL": "redis://redis-host:6379/1"})
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "redis://redis-host:6379/1")

    def test_valkey_host_components(self):
        """VALKEY_HOST + VALKEY_PORT + VALKEY_PASSWORD builds a redis:// URL."""
        info = self._probe(
            {
                "VALKEY_HOST": "myhost",
                "VALKEY_PORT": "6380",
                "VALKEY_DATABASE": "2",
            }
        )
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "redis://myhost:6380/2")

    def test_valkey_host_with_password(self):
        """VALKEY_PASSWORD is embedded in the URL."""
        info = self._probe(
            {
                "VALKEY_HOST": "myhost",
                "VALKEY_PASSWORD": "s3cret",
            }
        )
        self.assertEqual(info["cache_location"], "redis://:s3cret@myhost:6379/0")

    def test_sentinel_url(self):
        """VALKEY_URL=sentinel://... is passed through to vcache."""
        info = self._probe(
            {
                "VALKEY_URL": "sentinel://sentinel:26379/mymaster/0",
            }
        )
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "sentinel://sentinel:26379/mymaster/0")

    def test_sentinel_url_with_password(self):
        """Sentinel URL with embedded credentials."""
        info = self._probe(
            {
                "VALKEY_URL": "sentinel://:s3cret@s1:26379,s2:26379/mymaster/0",
            }
        )
        self.assertEqual(
            info["cache_location"],
            "sentinel://:s3cret@s1:26379,s2:26379/mymaster/0",
        )

    def test_tls_url(self):
        """rediss:// (TLS) is passed through to vcache."""
        info = self._probe({"VALKEY_URL": "rediss://secure-host:6380/0"})
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "rediss://secure-host:6380/0")

    def test_tls_with_ca_cert(self):
        """VALKEY_SSL_CA_CERTS populates OPTIONS."""
        info = self._probe(
            {
                "VALKEY_URL": "rediss://secure-host:6380/0",
                "VALKEY_SSL_CA_CERTS": "/etc/ssl/ca.crt",
            }
        )
        self.assertEqual(info["cache_options"]["ssl_ca_certs"], "/etc/ssl/ca.crt")

    def test_tls_mtls(self):
        """VALKEY_SSL_CERTFILE + VALKEY_SSL_KEYFILE for mTLS."""
        info = self._probe(
            {
                "VALKEY_URL": "rediss://secure-host:6380/0",
                "VALKEY_SSL_CA_CERTS": "/etc/ssl/ca.crt",
                "VALKEY_SSL_CERTFILE": "/etc/ssl/client.crt",
                "VALKEY_SSL_KEYFILE": "/etc/ssl/client.key",
            }
        )
        self.assertEqual(info["cache_options"]["ssl_ca_certs"], "/etc/ssl/ca.crt")
        self.assertEqual(info["cache_options"]["ssl_certfile"], "/etc/ssl/client.crt")
        self.assertEqual(info["cache_options"]["ssl_keyfile"], "/etc/ssl/client.key")

    def test_tls_skip_verification(self):
        """VALKEY_SSL_CERT_REQS=none to skip certificate verification."""
        info = self._probe(
            {
                "VALKEY_URL": "rediss://secure-host:6380/0",
                "VALKEY_SSL_CERT_REQS": "none",
            }
        )
        self.assertEqual(info["cache_options"]["ssl_cert_reqs"], "none")

    def test_no_tls_options_means_default_driver_only(self):
        """Without TLS env vars, OPTIONS carries only the gt_rust driver
        routing — always present: the cache and task broker run on gt_rust's
        single shared tokio runtime, and that wiring is not configurable."""
        info = self._probe({"VALKEY_URL": "redis://valkey:6379/0"})
        self.assertEqual(
            info["cache_options"],
            {"DRIVER_CLASS": "gt_rust.valkey.RustValkeyDriver"},
        )

    def test_empty_valkey_url_falls_back_to_db(self):
        """VALKEY_URL="" → database cache + DB task backend. The Rust DB
        engine (which also carries the Rust ingest path's cache and broker
        ops in this shape) is unconditional."""
        info = self._probe({"VALKEY_URL": ""})
        self.assertEqual(info["cache_backend"], self.DB_CACHE_BACKEND)
        self.assertEqual(info["task_backend"], self.VTASKS_DB)
        self.assertEqual(info["database_engine"], "gt_rust.django_backend")

    def test_no_valkey_env_defaults_to_vcache(self):
        """When no VALKEY_URL/REDIS_URL is set at all, settings.py defaults to
        redis://redis:6379/0 (docker compose default) → vcache.

        The only way to get DB fallback is to explicitly set VALKEY_URL="".
        """
        env = {
            "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
            "DATABASE_URL": self.BASE_ENV["DATABASE_URL"],
            "SECRET_KEY": "test-secret-key",
        }
        result = subprocess.run(
            [sys.executable, "-c", _SETTINGS_PROBE],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        info = json.loads(lines[-1])
        self.assertEqual(info["cache_backend"], self.VCACHE_BACKEND)
        self.assertEqual(info["cache_location"], "redis://redis:6379/0")


class PrepareThresholdConfigTestCase(TestCase):
    """DATABASE_PREPARE_THRESHOLD -> DATABASES["default"]["OPTIONS"].

    Boots settings.py in a subprocess the way CacheConfigTestCase does,
    because the value resolves at import time.
    """

    BASE_ENV = CacheConfigTestCase.BASE_ENV

    def _probe(self, value: str | None) -> subprocess.CompletedProcess:
        env = {} if value is None else {"DATABASE_PREPARE_THRESHOLD": value}
        return subprocess.run(
            [sys.executable, "-c", _SETTINGS_PROBE],
            capture_output=True,
            text=True,
            env={**self.BASE_ENV, **env},
            timeout=30,
            check=False,
        )

    def test_prepare_threshold(self):
        # Unset must stay opted in: the driver's own default is off, so losing
        # this silently regresses database CPU. 0 is psycopg's "prepare on
        # first use" rather than "disabled", so it has to survive the
        # off-value check instead of being read as falsy.
        for value, expected in ((None, 5), ("off", None), ("0", 0)):
            with self.subTest(value=value):
                result = self._probe(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                info = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(info["prepare_threshold"], expected)

        # An unparseable value must abort startup and name the variable, not
        # quietly fall back to a threshold nobody asked for.
        result = self._probe("sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_PREPARE_THRESHOLD", result.stderr)


class DatabaseIamAuthConfigTestCase(TestCase):
    """DATABASE_IAM_AUTH* -> DATABASES[...]["OPTIONS"], at settings-import time.

    glitchtip.test_db_iam covers the policy function in isolation; this covers
    the plumbing that decides whether the policy runs at all. That plumbing is
    where a misconfiguration turns the feature into a silent no-op, so it has
    to be exercised the way it actually resolves: in a fresh interpreter.
    """

    BASE_ENV = CacheConfigTestCase.BASE_ENV
    CA_BUNDLE = "/certs/rds-global-bundle.pem"
    PROVIDER = "django_vpg.contrib.aws.rds_iam_token"

    def _probe(self, extra_env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", _SETTINGS_PROBE],
            capture_output=True,
            text=True,
            env={**self.BASE_ENV, **extra_env},
            timeout=30,
            check=False,
        )

    def _info(self, extra_env: dict) -> dict:
        result = self._probe(extra_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_off_by_default_changes_nothing(self):
        # The shipped compose/Helm config must keep working untouched, with no
        # TLS keys appearing that the operator never asked for.
        info = self._info({})
        self.assertEqual(info["iam_provider"], "absent")
        self.assertEqual(info["sslmode"], "absent")

    def test_enabled_wires_the_provider(self):
        info = self._info(
            {"DATABASE_IAM_AUTH": "true", "DATABASE_SSLROOTCERT": self.CA_BUNDLE}
        )
        self.assertEqual(info["iam_provider"], self.PROVIDER)
        self.assertEqual(info["sslmode"], "verify-full")

    def test_an_empty_alias_list_aborts_rather_than_doing_nothing(self):
        # An env var set but empty reads as "" rather than falling back to the
        # default, which is the normal rendering of an unset Helm value. That
        # left every alias on password auth with a clean boot, so this is the
        # regression test for the whole feature being silently inert.
        for value in ("", " ", ",,"):
            with self.subTest(value=value):
                result = self._probe(
                    {
                        "DATABASE_IAM_AUTH": "true",
                        "DATABASE_IAM_AUTH_ALIASES": value,
                        "DATABASE_SSLROOTCERT": self.CA_BUNDLE,
                    }
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("DATABASE_IAM_AUTH_ALIASES", result.stderr)

    def test_an_unknown_alias_aborts(self):
        result = self._probe(
            {
                "DATABASE_IAM_AUTH": "true",
                "DATABASE_IAM_AUTH_ALIASES": "default,typo",
                "DATABASE_SSLROOTCERT": self.CA_BUNDLE,
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("typo", result.stderr)

    def test_an_alias_outside_the_list_keeps_its_password(self):
        # Naming only another alias must not relax the password requirement
        # here: an empty password against a trust-configured server connects
        # unauthenticated.
        result = self._probe(
            {
                "DATABASE_IAM_AUTH": "true",
                "DATABASE_IAM_AUTH_ALIASES": "default",
                "DATABASE_SSLROOTCERT": self.CA_BUNDLE,
                "DATABASE_URL": "",
                "DATABASE_HOST": "db.abc123.us-east-2.rds.amazonaws.com",
                "DATABASE_USER": "app_iam",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(info["password"], "")

        result = self._probe(
            {
                "DATABASE_IAM_AUTH": "true",
                "DATABASE_IAM_AUTH_ALIASES": "read_only",
                "DATABASE_SSLROOTCERT": self.CA_BUNDLE,
                "DATABASE_URL": "",
                "DATABASE_HOST": "db.abc123.us-east-2.rds.amazonaws.com",
                "DATABASE_USER": "app_iam",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_PASSWORD", result.stderr)

    def test_an_unverified_sslmode_aborts_without_the_opt_out(self):
        result = self._probe(
            {"DATABASE_IAM_AUTH": "true", "DATABASE_SSLMODE": "require"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_IAM_ALLOW_UNVERIFIED_TLS", result.stderr)

    def test_a_missing_ca_bundle_aborts(self):
        result = self._probe({"DATABASE_IAM_AUTH": "true"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DATABASE_SSLROOTCERT", result.stderr)


# ----------------------------------------------------------------------------
# Settings-import-time behaviour (cookie derivation, production warnings).
#
# These assertions run against `glitchtip/settings.py` as loaded from a fresh
# subprocess so that module-level side effects (warnings, env-driven defaults)
# are observed cleanly, independent of the test runner's own settings import.
# ----------------------------------------------------------------------------


def _load_deploy_settings(
    env_overrides: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run a subprocess that imports Django settings under the given env and
    emits a short report on stdout (settings values) and stderr (warnings).
    """
    code = (
        "import django; django.setup();"
        "from django.conf import settings;"
        "print('SESSION_COOKIE_SECURE', settings.SESSION_COOKIE_SECURE);"
        "print('CSRF_COOKIE_SECURE', settings.CSRF_COOKIE_SECURE);"
        "print('SECRET_KEY_IS_DEFAULT', settings.SECRET_KEY == 'change_me');"
        "print('ALLOWED_HOSTS', settings.ALLOWED_HOSTS);"
        "print('CONN_MAX_AGE', settings.DATABASES['default']['CONN_MAX_AGE']);"
        "print('POOL', bool(settings.DATABASES['default']['OPTIONS'].get('pool')));"
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
        # Minimum viable env. Subclasses override via env_overrides.
        "DEBUG": "false",
        "SECRET_KEY": "test-secret-key-not-default",
        "DATABASE_URL": "postgres://postgres:postgres@localhost:5432/postgres",
        "GLITCHTIP_URL": "https://example.com",
        "ALLOWED_HOSTS": "example.com",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-W", "always", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _parse_deploy_report(stdout: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in stdout.strip().splitlines() if line)


class PersistentConnectionsTests(SimpleTestCase):
    """Persistent connections went with WSGI serving.

    CONN_MAX_AGE is assigned rather than defaulted, which also protects the
    pooling configured just below it: a non-zero value used to switch that
    pooling off as a side effect.
    """

    def test_connections_are_pooled_and_not_persistent(self):
        r = _load_deploy_settings({})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_deploy_report(r.stdout)
        self.assertEqual(parsed["CONN_MAX_AGE"], "0")
        self.assertEqual(parsed["POOL"], "True")

    def test_retired_variable_no_longer_takes_effect(self):
        r = _load_deploy_settings({"DATABASE_CONN_MAX_AGE": "60"})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_deploy_report(r.stdout)
        self.assertEqual(parsed["CONN_MAX_AGE"], "0")
        self.assertEqual(parsed["POOL"], "True")


class CookieSecureDefaultsTests(SimpleTestCase):
    def test_https_url_defaults_cookies_to_secure(self):
        r = _load_deploy_settings({"GLITCHTIP_URL": "https://example.com"})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_deploy_report(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "True")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "True")

    def test_http_url_defaults_cookies_to_insecure(self):
        r = _load_deploy_settings({"GLITCHTIP_URL": "http://localhost:8000"})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_deploy_report(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "False")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "False")

    def test_env_override_wins_over_derived_default(self):
        r = _load_deploy_settings(
            {
                "GLITCHTIP_URL": "https://example.com",
                "SESSION_COOKIE_SECURE": "false",
                "CSRF_COOKIE_SECURE": "false",
            }
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_deploy_report(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "False")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "False")


class ProductionWarningTests(SimpleTestCase):
    def test_default_secret_key_warns_in_production(self):
        r = _load_deploy_settings({"SECRET_KEY": "change_me", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_default_secret_key_silent_in_debug(self):
        r = _load_deploy_settings({"SECRET_KEY": "change_me", "DEBUG": "true"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_real_secret_key_silent(self):
        r = _load_deploy_settings(
            {"SECRET_KEY": "real-secret-abc123xyz789", "DEBUG": "false"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_wildcard_allowed_hosts_warns_in_production(self):
        r = _load_deploy_settings({"ALLOWED_HOSTS": "*", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ALLOWED_HOSTS is the wildcard default", r.stderr)

    def test_wildcard_allowed_hosts_silent_in_debug(self):
        r = _load_deploy_settings({"ALLOWED_HOSTS": "*", "DEBUG": "true"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ALLOWED_HOSTS is the wildcard default", r.stderr)

    def test_scoped_allowed_hosts_silent(self):
        r = _load_deploy_settings(
            {"ALLOWED_HOSTS": "glitchtip.example.com", "DEBUG": "false"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ALLOWED_HOSTS is the wildcard default", r.stderr)


class AsyncConnectionRecyclingTestCase(SimpleTestCase):
    """``recycle_async_connections`` has to close the connection *and*
    put a fresh wrapper in the store: django-async-backend stamps a
    wrapper with the task that first used it and never clears the stamp,
    so a closed-but-kept wrapper is refused to every later task."""

    def _claimed_wrapper(self):
        """A wrapper in the state a request leaves behind: stamped by some
        task, with a close that does not need a database."""
        conn = async_connections["default"]
        conn._task = asyncio.current_task()
        conn.close = AsyncMock()
        return conn

    async def test_recycles_a_claimed_alias(self):
        original = self._claimed_wrapper()

        await recycle_async_connections()

        original.close.assert_awaited_once()
        self.assertIsNot(async_connections["default"], original)
        # Unstamped, so the next task may claim it.
        self.assertIsNone(async_connections["default"]._task)

    async def test_leaves_an_untouched_alias_alone(self):
        """A wrapper nobody claimed and nobody opened has nothing to
        reclaim; replacing it would churn an object per request."""
        untouched = async_connections["default"]
        untouched.close = AsyncMock()

        await recycle_async_connections()

        untouched.close.assert_not_awaited()
        self.assertIs(async_connections["default"], untouched)

    async def test_recycles_despite_a_failing_close(self):
        """A connection that can't be returned (a failover mid-request)
        must still be dropped, or the alias stays stamped for good."""
        original = self._claimed_wrapper()
        original.close = AsyncMock(side_effect=OSError("connection reset"))

        with self.assertLogs("glitchtip.async_db", level="ERROR"):
            await recycle_async_connections()

        self.assertIsNot(async_connections["default"], original)

    async def test_skips_an_alias_inside_a_transaction(self):
        """Swapping the store entry mid-transaction would let the atomic
        block commit a wrapper that never saw the writes."""
        original = self._claimed_wrapper()
        original.in_atomic_block = True
        # TESTING calls logging.disable(WARNING); lift it so the skip's
        # own warning is observable here.
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.WARNING)

        with self.assertLogs("glitchtip.async_db", level="WARNING"):
            await recycle_async_connections()

        original.close.assert_not_awaited()
        self.assertIs(async_connections["default"], original)

    async def test_cancellation_still_unclaims_and_closes(self):
        """A client that disconnects mid-response cancels the recycle. The
        alias must come back unclaimed anyway, and the close must finish:
        a stamped wrapper breaks every later task on that alias."""
        original = self._claimed_wrapper()
        closing = asyncio.Event()
        release = asyncio.Event()
        closed = asyncio.Event()
        entry = {}

        async def slow_close():
            closing.set()
            await release.wait()
            closed.set()

        original.close = slow_close

        async def recycle():
            try:
                await recycle_async_connections()
            finally:
                # Read from inside the child task: its context is a copy,
                # so the swap is only visible here.
                entry["after"] = async_connections["default"]

        task = asyncio.create_task(recycle())
        # Bounded: an implementation that never closes would otherwise
        # hang the suite here rather than failing it.
        await asyncio.wait_for(closing.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The store write happens before the first await, so the swap
        # survived the cancellation...
        self.assertIsNot(entry["after"], original)
        # ...and the shielded close kept running rather than being dropped.
        release.set()
        await asyncio.wait_for(closed.wait(), timeout=5)


class RequestRecyclingTestCase(SimpleTestCase):
    """django-async-backend's own request receivers close without
    unstamping — and make every response pay a Signal.send hop — so they
    are switched off and a middleware takes their place."""

    def test_async_backend_request_receivers_are_not_connected(self):
        for signal in (signals.request_started, signals.request_finished):
            # weak=False upstream, so the entry holds the function itself.
            receivers = [entry[1] for entry in signal.receivers]
            self.assertNotIn(close_old_async_connections, receivers)

    def test_middleware_is_in_both_chains(self):
        self.assertIn(MIDDLEWARE_PATH, settings.MIDDLEWARE)
        self.assertIn(MIDDLEWARE_PATH, INGEST_MIDDLEWARE)

    def test_system_check_reports_a_missing_middleware(self):
        """The check is the guard that the wiring above still exists; it
        has to actually fail when the wiring is gone."""
        self.assertEqual(check_async_connection_recycling(), [])
        with self.settings(MIDDLEWARE=[]):
            errors = check_async_connection_recycling()
        self.assertEqual([error.id for error in errors], ["glitchtip.E001"])

    async def test_middleware_recycles_around_the_response(self):
        original = async_connections["default"]
        original._task = asyncio.current_task()
        original.close = AsyncMock()
        seen = {}

        async def get_response(request):
            seen["during"] = async_connections["default"]
            return "response"

        middleware = recycle_async_connections_middleware(get_response)
        self.assertEqual(await middleware(None), "response")

        # Recycled on the way in, so the view never inherits a claimed
        # wrapper, and again on the way out.
        self.assertIsNot(seen["during"], original)
        self.assertIsNot(async_connections["default"], original)
        original.close.assert_awaited_once()


class TaskSignalsTestCase(SimpleTestCase):
    """The worker has no HTTP request cycle, so async DB connections
    opened by an async task would leak — and stay stamped with that
    task. ``glitchtip.task_signals`` connects a receiver to vtasks'
    ``task_finished`` / ``task_failure`` signals that recycles
    ``async_connections`` the way the request middleware does."""

    async def _assert_signal_recycles_async_connections(self, signal, **payload):
        original = async_connections["default"]
        original._task = asyncio.current_task()
        original.close = AsyncMock()

        await signal.asend(sender=type(self), **payload)

        original.close.assert_awaited_once()
        # asend dispatches the receiver in a child task; this also proves
        # its context changes are restored into ours.
        self.assertIsNot(async_connections["default"], original)

    async def test_task_finished_recycles_async_connections(self):
        await self._assert_signal_recycles_async_connections(
            task_finished,
            task_id="x",
            task_ids=None,
            name="glitchtip.tests.fake_task",
            duration=0.0,
        )

    async def test_task_failure_recycles_async_connections(self):
        await self._assert_signal_recycles_async_connections(
            task_failure,
            task_id="x",
            task_ids=None,
            name="glitchtip.tests.fake_task",
            exception=RuntimeError("boom"),
            traceback="",
        )
