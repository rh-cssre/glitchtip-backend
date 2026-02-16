"""
Tests for cold storage functionality with standalone DuckDB.

DuckDB runs in-process (no PostgreSQL extension required).
Tests that need S3 access are skipped when no bucket is configured.
"""

from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from glitchtip.cold_storage import get_cold_storage_backend, is_duckdb_available
from glitchtip.partition_manager import UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

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
        AWS_STORAGE_BUCKET_NAME="my-bucket",
    )
    def test_override_takes_precedence_over_bucket(self):
        """Explicit false overrides auto-detection from bucket config."""
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET="cold-bucket",
    )
    def test_auto_enabled_with_cold_storage_bucket(self):
        self.assertTrue(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME="my-bucket",
    )
    def test_auto_enabled_with_aws_bucket(self):
        self.assertTrue(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_disabled_without_bucket(self):
        """No bucket or directory configured = no cold storage."""
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        GLITCHTIP_COLD_STORAGE_DIR="/tmp/cold",
    )
    def test_auto_enabled_with_cold_storage_dir(self):
        self.assertTrue(is_duckdb_available())


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
        from ..cold_storage import get_duckdb_connection

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
        AWS_STORAGE_BUCKET_NAME=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_no_backend_configured(self):
        backend = get_cold_storage_backend()
        self.assertIsNone(backend)

    @override_settings(
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
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
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from ..api import query_cold_storage

        now = datetime.now(dt_timezone.utc)
        start = now - timedelta(days=1)

        results = query_cold_storage(
            organization_id=1,
            start_dt=start,
            end_dt=now,
        )
        self.assertEqual(results, [])

    def test_api_cold_storage_returns_empty_when_disabled(self):
        """Test that API cold storage returns empty when disabled."""
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from ..api import query_cold_storage

        now = datetime.now(dt_timezone.utc)
        start = now - timedelta(days=1)

        results = query_cold_storage(
            organization_id=1,
            start_dt=start,
            end_dt=now,
        )
        self.assertEqual(results, [])
