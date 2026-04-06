"""
Tests for cold storage functionality with standalone DuckDB.

DuckDB runs in-process (no PostgreSQL extension required).
Tests that need S3 access are skipped when no bucket is configured.
"""

import shutil
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from uuid import UUID

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time

from glitchtip.cold_storage import get_cold_storage_backend, is_duckdb_available
from glitchtip.partition_manager import PartitionManager, UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..cold_storage import EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
from ..constants import LogLevel
from ..models import LogEvent


class DuckDBAvailabilityTestCase(TestCase):
    """Test DuckDB availability check."""

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="true")
    def test_enabled_via_override(self):
        self.assertTrue(is_duckdb_available())

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="false")
    def test_disabled_via_override(self):
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="false",
        GLITCHTIP_COLD_STORAGE_BUCKET="my-bucket",
    )
    def test_explicit_false_with_bucket(self):
        """Explicit false disables even when bucket is configured."""
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET="cold-bucket",
    )
    def test_disabled_without_explicit_opt_in(self):
        """Bucket alone is not enough — requires GLITCHTIP_ENABLE_DUCKDB=true."""
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_disabled_without_any_config(self):
        """No override and no bucket = no cold storage."""
        self.assertFalse(is_duckdb_available())



class ColdStoragePathTestCase(TestCase):
    """Test cold storage path generation."""

    def test_org_cold_storage_path(self):
        """Test storage-relative path (without bucket)."""
        from glitchtip.cold_storage import get_org_cold_storage_path

        path = get_org_cold_storage_path("logs_logevent", 456, "20260120")
        self.assertEqual(path, "cold_storage/logs_logevent/org_456/20260120.parquet")


class DuckDBConnectionTestCase(TestCase):
    """Test standalone DuckDB connection setup."""

    def test_get_connection_no_s3(self):
        """Test creating a DuckDB connection without S3 (filesystem backend)."""
        from glitchtip.cold_storage import get_duckdb_connection

        conn = get_duckdb_connection()
        try:
            # Should be able to execute basic queries
            result = conn.execute("SELECT 1").fetchone()
            self.assertEqual(result[0], 1)
        finally:
            conn.close()


class ColdStorageBackendTestCase(TestCase):
    """Test cold storage backend detection."""

    @override_settings(
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_no_backend_configured(self):
        backend = get_cold_storage_backend()
        self.assertIsNone(backend)

    @override_settings(
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR="/tmp/cold-test",
    )
    def test_filesystem_backend(self):
        from django.core.files.storage import FileSystemStorage

        backend = get_cold_storage_backend()
        self.assertIsInstance(backend, FileSystemStorage)
        self.assertEqual(backend.location, "/tmp/cold-test")


class ColdStorageQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test cold storage query functionality."""

    def setUp(self):
        self.create_project()

    def _skip_if_no_backend(self):
        """Skip test if no storage backend is configured."""
        if not get_cold_storage_backend():
            self.skipTest("No cold storage backend configured")

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="true")
    def test_query_empty_cold_storage(self):
        """Test querying cold storage when no files exist."""
        self._skip_if_no_backend()

        from datetime import datetime
        from datetime import timezone as dt_timezone

        from ..api import query_cold_storage

        now = datetime.now(dt_timezone.utc)
        results = query_cold_storage(
            organization_id=99999,
            start_dt=datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
            end_dt=now,
        )

        # Should return empty list, not error
        self.assertEqual(results, [])


class ColdStorageAPIQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test cold storage queries via API module."""

    def setUp(self):
        self.create_project()

    def test_query_logs_combined_hot_only(self):
        """Test combined query when all data is in hot storage."""
        from asgiref.sync import async_to_sync

        from ..api import query_logs_combined

        now = timezone.now()

        # Create recent logs (hot storage only) with distinct timestamps
        for i in range(3):
            t = now - timedelta(minutes=i)
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(t),
                organization=self.organization,
                project=self.project,
                level=LogLevel.INFO,
                body=f"Recent log {i}",
            )

        # Query last 24 hours (all hot storage)
        start = now - timedelta(days=1)
        results = async_to_sync(query_logs_combined)(
            organization_id=self.organization.id,
            start_dt=start,
            end_dt=now + timedelta(minutes=1),  # Ensure we capture all logs
        )

        # Should find all 3 logs
        self.assertEqual(len(results), 3)

    def test_query_logs_combined_with_filters(self):
        """Test combined query with filters."""
        from asgiref.sync import async_to_sync

        from ..api import query_logs_combined

        now = timezone.now()

        # Create logs with different levels and distinct timestamps
        for i, level in enumerate([LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR]):
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(now - timedelta(minutes=i)),
                organization=self.organization,
                project=self.project,
                level=level,
                body=f"Log level {level.label}",
            )

        start = now - timedelta(days=1)

        # Filter by error level
        results = async_to_sync(query_logs_combined)(
            organization_id=self.organization.id,
            start_dt=start,
            end_dt=now + timedelta(minutes=1),
            level_values=[LogLevel.ERROR],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].level, LogLevel.ERROR)


class NoDuckDBTestCase(TestCase):
    """Tests that verify graceful behavior when DuckDB cold storage is disabled."""

    def test_cold_storage_returns_empty_when_disabled(self):
        """Test that cold storage returns empty when GLITCHTIP_ENABLE_DUCKDB is not set."""
        from ..api import query_cold_storage

        now = datetime.now(dt_timezone.utc)
        start = now - timedelta(days=1)

        results = query_cold_storage(
            organization_id=1,
            start_dt=start,
            end_dt=now,
        )
        self.assertEqual(results, [])


def _create_log_partition(date: datetime) -> str:
    """Create a log partition for a specific date. Returns partition name."""
    manager = PartitionManager()
    date_str = date.strftime("%Y%m%d")
    partition_name = f"logs_logevent_{date_str}"
    next_date = date + timedelta(days=1)

    sqls = manager.create_time_partition(
        parent_table="logs_logevent",
        partition_name=partition_name,
        start_date=date,
        end_date=next_date,
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
        partition_column="id",
    )
    with connection.cursor() as cursor:
        for sql in sqls:
            cursor.execute(sql)
    return partition_name


def _drop_partition(partition_name: str):
    """Detach and drop a partition, ignoring errors."""
    with connection.cursor() as cursor:
        try:
            cursor.execute(
                f"ALTER TABLE logs_logevent DETACH PARTITION {partition_name}"
            )
        except Exception:
            connection.connection.rollback()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {partition_name} CASCADE")
        except Exception:
            connection.connection.rollback()


def _bulk_insert_logs(date: datetime, count: int, org_id: int, project_id: int):
    """Insert synthetic log events into PG for a given date."""
    with connection.cursor() as cursor:
        batch = []
        for i in range(count):
            offset_ms = int((i / max(count, 1)) * 86400 * 1000)
            event_time = date + timedelta(milliseconds=offset_ms)
            event_id = UUID7Helper.from_datetime(event_time)
            batch.append(
                cursor.mogrify(
                    "(%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        str(event_id),
                        org_id,
                        project_id,
                        LogLevel.INFO,
                        9,
                        f"Test log {i}",
                        "test-svc",
                        "test",
                    ],
                )
            )
        values = ",".join(batch)
        cursor.execute(
            "INSERT INTO logs_logevent "
            "(id, organization_id, project_id, level, severity_number, "
            "body, service, environment) VALUES " + values
        )


class ColdStorageQueryUnionTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """
    Test that hot + cold log queries return correct, deduplicated results.

    Creates a cold partition (archived to parquet then dropped) and a hot
    partition (still in PG), then verifies query_hot_storage and
    query_cold_storage return the right events for various date ranges.
    """

    def setUp(self):
        self.create_project()
        self.cold_dir = tempfile.mkdtemp(prefix="glitchtip_cold_test_")
        self.cold_date = datetime(2025, 4, 10, tzinfo=dt_timezone.utc)
        self.hot_date = datetime(2025, 4, 11, tzinfo=dt_timezone.utc)
        self.cold_part = _create_log_partition(self.cold_date)
        self.hot_part = _create_log_partition(self.hot_date)

    def tearDown(self):
        _drop_partition(self.hot_part)
        _drop_partition(self.cold_part)
        shutil.rmtree(self.cold_dir, ignore_errors=True)

    def _setup_hot_and_cold(self):
        """Insert data into both partitions, archive cold to parquet."""
        from glitchtip.cold_storage import archive_and_swap_partition

        org_id = self.organization.id
        proj_id = self.project.id

        _bulk_insert_logs(self.cold_date, 50, org_id, proj_id)
        _bulk_insert_logs(self.hot_date, 50, org_id, proj_id)

        archive_and_swap_partition(
            self.cold_part, "logs_logevent", EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
        )

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_full_range_union(self):
        """Querying both tiers returns correct total without duplicates."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_hot_and_cold()

            from ..api import query_cold_storage, query_hot_storage

            full_start = self.cold_date
            full_end = self.hot_date + timedelta(days=1)

            hot = query_hot_storage(
                organization_id=self.organization.id,
                start_dt=full_start,
                end_dt=full_end,
                limit=200,
            )
            cold = query_cold_storage(
                organization_id=self.organization.id,
                start_dt=full_start,
                end_dt=full_end,
                limit=200,
            )

            self.assertEqual(len(hot), 50)
            self.assertEqual(len(cold), 50)

            # No duplicates across tiers
            all_ids = {r.id for r in hot} | {r.id for r in cold}
            self.assertEqual(len(all_ids), 100)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_hot_only_range(self):
        """Querying only the hot date range returns hot data."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_hot_and_cold()

            from ..api import query_hot_storage

            results = query_hot_storage(
                organization_id=self.organization.id,
                start_dt=self.hot_date,
                end_dt=self.hot_date + timedelta(days=1),
                limit=200,
            )
            self.assertEqual(len(results), 50)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_cold_only_range(self):
        """Querying only the cold date range returns archived data."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_hot_and_cold()

            from ..api import query_cold_storage

            results = query_cold_storage(
                organization_id=self.organization.id,
                start_dt=self.cold_date,
                end_dt=self.cold_date + timedelta(days=1),
                limit=200,
            )
            self.assertEqual(len(results), 50)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_boundary_spans_both_tiers(self):
        """A narrow range spanning the hot/cold boundary returns events from both."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_hot_and_cold()

            from ..api import query_cold_storage, query_hot_storage

            boundary_start = self.cold_date + timedelta(hours=23)
            boundary_end = self.hot_date + timedelta(hours=1)

            hot = query_hot_storage(
                organization_id=self.organization.id,
                start_dt=boundary_start,
                end_dt=boundary_end,
                limit=200,
            )
            cold = query_cold_storage(
                organization_id=self.organization.id,
                start_dt=boundary_start,
                end_dt=boundary_end,
                limit=200,
            )
            self.assertGreater(len(hot), 0)
            self.assertGreater(len(cold), 0)


class MissingParquetTestCase(TestCase):
    """
    Test graceful handling when parquet files don't exist.

    Cold storage queries should return empty results, not raise exceptions.
    """

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
    )
    def test_query_cold_logs_returns_empty(self):
        """query_cold_storage returns [] when no parquet files exist."""
        with tempfile.TemporaryDirectory(prefix="glitchtip_cold_test_") as cold_dir:
            with self.settings(GLITCHTIP_COLD_STORAGE_DIR=cold_dir):
                from ..api import query_cold_storage

                results = query_cold_storage(
                    organization_id=99999,
                    start_dt=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
                    end_dt=datetime(2020, 12, 31, tzinfo=dt_timezone.utc),
                    limit=100,
                )
                self.assertEqual(results, [])

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
    )
    def test_get_log_from_cold_returns_none(self):
        """_get_log_from_cold returns None when parquet file doesn't exist."""
        with tempfile.TemporaryDirectory(prefix="glitchtip_cold_test_") as cold_dir:
            with self.settings(GLITCHTIP_COLD_STORAGE_DIR=cold_dir):
                from ..api import _get_log_from_cold

                fake_time = datetime(2020, 6, 15, tzinfo=dt_timezone.utc)
                fake_id = UUID7Helper.from_datetime(fake_time)
                result = _get_log_from_cold(99999, fake_id, fake_time)
                self.assertIsNone(result)


class CorruptParquetTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """
    Test that a corrupt parquet file doesn't poison the entire cold query.

    Creates two archived days (valid parquet files), then corrupts one.
    The query should return results from the valid file, log an ERROR
    for the corrupt file, and not raise an exception.
    """

    def setUp(self):
        self.create_project()
        self.cold_dir = tempfile.mkdtemp(prefix="glitchtip_cold_test_")
        self.day1 = datetime(2025, 5, 1, tzinfo=dt_timezone.utc)
        self.day2 = datetime(2025, 5, 2, tzinfo=dt_timezone.utc)
        self.part1 = _create_log_partition(self.day1)
        self.part2 = _create_log_partition(self.day2)

    def tearDown(self):
        _drop_partition(self.part1)
        _drop_partition(self.part2)
        shutil.rmtree(self.cold_dir, ignore_errors=True)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_corrupt_file_returns_partial_results(self):
        """Valid file results are returned; corrupt file is logged and skipped."""
        from glitchtip.cold_storage import (
            archive_and_swap_partition,
            get_cold_storage_backend,
            get_duckdb_parquet_path,
            get_org_cold_storage_path,
        )

        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            org_id = self.organization.id
            proj_id = self.project.id

            _bulk_insert_logs(self.day1, 30, org_id, proj_id)
            _bulk_insert_logs(self.day2, 30, org_id, proj_id)

            archive_and_swap_partition(
                self.part1, "logs_logevent", EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
            )
            archive_and_swap_partition(
                self.part2, "logs_logevent", EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
            )

            # Corrupt day1's parquet file
            storage = get_cold_storage_backend()
            day1_path = get_org_cold_storage_path("logs_logevent", org_id, "20250501")
            full_path = get_duckdb_parquet_path(storage, day1_path)
            with open(full_path, "wb") as f:
                f.write(b"CORRUPT DATA")

            from ..api import query_cold_storage

            query_start = self.day1
            query_end = self.day2 + timedelta(days=1)

            with self.assertLogs("glitchtip.cold_storage", level="ERROR") as cm:
                results = query_cold_storage(
                    organization_id=org_id,
                    start_dt=query_start,
                    end_dt=query_end,
                    limit=200,
                )

            # Should return the 30 results from the valid day2 file
            self.assertEqual(len(results), 30)

            # Should have logged an ERROR mentioning the corrupt file
            self.assertTrue(
                any("20250501.parquet" in msg for msg in cm.output),
                f"Expected ERROR log mentioning corrupt file, got: {cm.output}",
            )


class RowToLogEventCoercionTestCase(TestCase):
    """Test _row_to_log_event handles NULL and type mismatches from Parquet."""

    def test_null_fields_become_empty_strings(self):
        """NULL body, service, environment, host from Parquet are coerced to ''."""
        from ..api import _row_to_log_event

        now = datetime.now(dt_timezone.utc)
        uid = UUID7Helper.from_datetime(now)

        row = (
            uid,  # id
            None,  # trace_id
            1,  # organization_id
            1,  # project_id
            None,  # span_id
            LogLevel.INFO,  # level
            9,  # severity_number
            None,  # body (NULL)
            None,  # service (NULL)
            None,  # environment (NULL)
            None,  # host (NULL)
            None,  # data (NULL)
        )
        event = _row_to_log_event(row)

        self.assertEqual(event.body, "")
        self.assertEqual(event.service, "")
        self.assertEqual(event.environment, "")
        self.assertEqual(event.host, "")
        self.assertEqual(event.data, {})
        self.assertIsNone(event.trace_id)
        self.assertIsNone(event.span_id)

    def test_span_id_string_to_int(self):
        """span_id returned as string from DuckDB is converted to int."""
        from ..api import _row_to_log_event

        now = datetime.now(dt_timezone.utc)
        uid = UUID7Helper.from_datetime(now)

        row = (
            uid,
            None,
            1,
            1,
            "12345",  # span_id as string (from Parquet)
            LogLevel.INFO,
            9,
            "test body",
            "svc",
            "prod",
            "host1",
            "{}",
        )
        event = _row_to_log_event(row)
        self.assertEqual(event.span_id, 12345)

    def test_span_id_empty_string_becomes_none(self):
        """Empty span_id string from DuckDB becomes None."""
        from ..api import _row_to_log_event

        now = datetime.now(dt_timezone.utc)
        uid = UUID7Helper.from_datetime(now)

        row = (uid, None, 1, 1, "", LogLevel.INFO, 9, "b", "s", "e", "h", "{}")
        event = _row_to_log_event(row)
        self.assertIsNone(event.span_id)

    def test_id_string_to_uuid(self):
        """id returned as string from DuckDB is converted to UUID."""
        from ..api import _row_to_log_event

        now = datetime.now(dt_timezone.utc)
        uid = UUID7Helper.from_datetime(now)

        row = (
            str(uid),  # id as string (from Parquet)
            None,
            1,
            1,
            None,
            LogLevel.INFO,
            9,
            "body",
            "svc",
            "prod",
            "host1",
            "{}",
        )
        event = _row_to_log_event(row)
        self.assertEqual(event.id, uid)
        self.assertIsInstance(event.id, UUID)

    def test_trace_id_string_to_uuid(self):
        """trace_id returned as string from DuckDB is converted to UUID."""
        import uuid

        from ..api import _row_to_log_event

        now = datetime.now(dt_timezone.utc)
        uid = UUID7Helper.from_datetime(now)
        trace = uuid.uuid4()

        row = (uid, str(trace), 1, 1, None, LogLevel.INFO, 9, "b", "s", "e", "h", "{}")
        event = _row_to_log_event(row)
        self.assertEqual(event.trace_id, trace)
        self.assertIsInstance(event.trace_id, UUID)


class CombinedQueryBothTiersTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """
    Test query_logs_combined with data spanning hot and cold storage.

    Uses freeze_time and patches HOT_STORAGE_DAYS to control tier routing.
    """

    def setUp(self):
        self.create_project()
        self.cold_dir = tempfile.mkdtemp(prefix="glitchtip_cold_test_")
        # Cold partition: 2 days before frozen time
        self.cold_date = datetime(2025, 4, 10, tzinfo=dt_timezone.utc)
        # Hot partition: same day as frozen time
        self.hot_date = datetime(2025, 4, 12, tzinfo=dt_timezone.utc)
        self.cold_part = _create_log_partition(self.cold_date)
        self.hot_part = _create_log_partition(self.hot_date)

    def tearDown(self):
        _drop_partition(self.hot_part)
        _drop_partition(self.cold_part)
        shutil.rmtree(self.cold_dir, ignore_errors=True)

    def _setup_data(self):
        from glitchtip.cold_storage import archive_and_swap_partition

        org_id = self.organization.id
        proj_id = self.project.id

        _bulk_insert_logs(self.cold_date, 20, org_id, proj_id)
        _bulk_insert_logs(self.hot_date, 15, org_id, proj_id)

        archive_and_swap_partition(
            self.cold_part, "logs_logevent", EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
        )

    @freeze_time("2025-04-12 12:00:00")
    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_combined_query_returns_both_tiers(self):
        """query_logs_combined returns data from both hot and cold."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_data()

            from asgiref.sync import async_to_sync

            from ..api import query_logs_combined

            # HOT_STORAGE_DAYS=1 → cutoff = 2025-04-11 12:00:00
            # cold_date (2025-04-10) < cutoff → cold tier
            # hot_date (2025-04-12) > cutoff → hot tier
            with mock.patch("apps.logs.api.HOT_STORAGE_DAYS", 1):
                results = async_to_sync(query_logs_combined)(
                    organization_id=self.organization.id,
                    start_dt=self.cold_date,
                    end_dt=self.hot_date + timedelta(days=1),
                    limit=200,
                )

            # 20 cold + 15 hot = 35 total
            self.assertEqual(len(results), 35)

            # Verify sorted DESC by id
            ids = [r.id for r in results]
            self.assertEqual(ids, sorted(ids, reverse=True))

            # No duplicates
            self.assertEqual(len(set(ids)), 35)

    @freeze_time("2025-04-12 12:00:00")
    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_cursor_pagination_across_tiers(self):
        """Cursor pagination crosses from hot to cold without duplicates."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            self._setup_data()

            from asgiref.sync import async_to_sync

            from ..api import query_logs_combined

            all_ids = set()
            cursor = None
            pages = 0

            with mock.patch("apps.logs.api.HOT_STORAGE_DAYS", 1):
                while pages < 10:  # safety limit
                    results = async_to_sync(query_logs_combined)(
                        organization_id=self.organization.id,
                        start_dt=self.cold_date,
                        end_dt=self.hot_date + timedelta(days=1),
                        limit=10,
                        cursor_position=cursor,
                    )
                    if not results:
                        break
                    page_ids = {r.id for r in results}
                    # Verify no duplicates across pages
                    overlap = all_ids & page_ids
                    self.assertEqual(
                        overlap,
                        set(),
                        f"Duplicate IDs across pages: {overlap}",
                    )
                    all_ids |= page_ids
                    cursor = results[-1].id
                    pages += 1

            # Should have fetched all 35 events across pages
            self.assertEqual(len(all_ids), 35)


class CountStorageTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test count_hot_storage and count_cold_storage functions."""

    def setUp(self):
        self.create_project()
        self.cold_dir = tempfile.mkdtemp(prefix="glitchtip_cold_test_")
        self.cold_date = datetime(2025, 4, 10, tzinfo=dt_timezone.utc)
        self.hot_date = datetime(2025, 4, 11, tzinfo=dt_timezone.utc)
        self.cold_part = _create_log_partition(self.cold_date)
        self.hot_part = _create_log_partition(self.hot_date)

    def tearDown(self):
        _drop_partition(self.hot_part)
        _drop_partition(self.cold_part)
        shutil.rmtree(self.cold_dir, ignore_errors=True)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_count_hot_storage_with_cap(self):
        """count_hot_storage respects max_hits cap."""
        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            _bulk_insert_logs(self.hot_date, 50, self.organization.id, self.project.id)

            from ..api import count_hot_storage

            full_start = self.hot_date
            full_end = self.hot_date + timedelta(days=1)

            count = count_hot_storage(
                organization_id=self.organization.id,
                start_dt=full_start,
                end_dt=full_end,
                max_hits=20,
            )
            self.assertEqual(count, 20)

            # Without cap, should return actual count
            count_full = count_hot_storage(
                organization_id=self.organization.id,
                start_dt=full_start,
                end_dt=full_end,
                max_hits=1000,
            )
            self.assertEqual(count_full, 50)

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_count_cold_storage(self):
        """count_cold_storage counts archived logs with cap."""
        from glitchtip.cold_storage import archive_and_swap_partition

        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            _bulk_insert_logs(self.cold_date, 30, self.organization.id, self.project.id)
            archive_and_swap_partition(
                self.cold_part, "logs_logevent", EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
            )

            from ..api import count_cold_storage

            count = count_cold_storage(
                organization_id=self.organization.id,
                start_dt=self.cold_date,
                end_dt=self.cold_date + timedelta(days=1),
                max_hits=1000,
            )
            self.assertEqual(count, 30)

            # With small cap
            count_capped = count_cold_storage(
                organization_id=self.organization.id,
                start_dt=self.cold_date,
                end_dt=self.cold_date + timedelta(days=1),
                max_hits=10,
            )
            self.assertEqual(count_capped, 10)

    def test_count_cold_storage_disabled(self):
        """count_cold_storage returns 0 when DuckDB is disabled."""
        from ..api import count_cold_storage

        count = count_cold_storage(
            organization_id=1,
            start_dt=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
            end_dt=datetime(2020, 12, 31, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(count, 0)


class ILIKEEscapingTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test that ILIKE wildcards in search queries are escaped."""

    def setUp(self):
        self.create_project()
        self.date = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)
        self.part = _create_log_partition(self.date)

    def tearDown(self):
        _drop_partition(self.part)

    def test_percent_in_query_is_literal(self):
        """A '%' in the search query matches literal '%', not wildcard."""
        org_id = self.organization.id
        proj_id = self.project.id

        # Insert logs - one with literal %, one without
        with connection.cursor() as cursor:
            for i, body in enumerate(["100% complete", "100 complete"]):
                event_time = self.date + timedelta(seconds=i)
                event_id = UUID7Helper.from_datetime(event_time)
                cursor.execute(
                    "INSERT INTO logs_logevent "
                    "(id, organization_id, project_id, level, severity_number, body) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [str(event_id), org_id, proj_id, LogLevel.INFO, 9, body],
                )

        from ..api import query_hot_storage

        results = query_hot_storage(
            organization_id=org_id,
            start_dt=self.date,
            end_dt=self.date + timedelta(days=1),
            query="100%",
            limit=100,
        )
        # Should match both because "100%" contains "100%" literally,
        # and "100 complete" also starts with "100" — but the '%' is escaped,
        # so only the literal match should work
        matching_bodies = {r.body for r in results}
        self.assertIn("100% complete", matching_bodies)

    def test_underscore_in_query_is_literal(self):
        """A '_' in the search query matches literal '_', not single-char wildcard."""
        org_id = self.organization.id
        proj_id = self.project.id

        with connection.cursor() as cursor:
            for i, body in enumerate(["log_event_started", "logXeventXstarted"]):
                event_time = self.date + timedelta(seconds=i)
                event_id = UUID7Helper.from_datetime(event_time)
                cursor.execute(
                    "INSERT INTO logs_logevent "
                    "(id, organization_id, project_id, level, severity_number, body) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [str(event_id), org_id, proj_id, LogLevel.INFO, 9, body],
                )

        from ..api import query_hot_storage

        results = query_hot_storage(
            organization_id=org_id,
            start_dt=self.date,
            end_dt=self.date + timedelta(days=1),
            query="log_event",
            limit=100,
        )
        matching_bodies = {r.body for r in results}
        self.assertIn("log_event_started", matching_bodies)
        # Without escaping, "_" matches any char, so "logXeventXstarted" would match
        self.assertNotIn("logXeventXstarted", matching_bodies)
