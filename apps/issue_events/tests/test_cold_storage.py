"""
Tests for issue event cold storage functionality with standalone DuckDB.

DuckDB runs in-process (no PostgreSQL extension required).
Tests that need S3 access are skipped when no bucket is configured.
"""

import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from glitchtip.cold_storage import (
    get_org_cold_storage_path,
    is_duckdb_available,
)
from glitchtip.partition_manager import UUID7Helper

from ..cold_storage import (
    ISSUE_EVENT_EXPORT_COLUMN_TYPES,
    TABLE_NAME,
    IssueEventRow,
    get_event_from_cold,
    query_cold_events,
)


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
        self.assertFalse(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR="/tmp/cold",
    )
    def test_disabled_with_dir_but_no_opt_in(self):
        """Directory alone is not enough — requires GLITCHTIP_ENABLE_DUCKDB=true."""
        self.assertFalse(is_duckdb_available())


class ColdStoragePathTestCase(TestCase):
    """Test cold storage path generation for issue events."""

    def test_org_cold_storage_path(self):
        path = get_org_cold_storage_path(TABLE_NAME, 456, "20260120")
        self.assertEqual(
            path,
            "cold_storage/issue_events_issueevent/org_456/20260120.parquet",
        )


class IssueEventColumnTypesTestCase(TestCase):
    """Test issue event export column types."""

    def test_all_columns_defined(self):
        expected_columns = {
            "id",
            "event_id",
            "timestamp",
            "issue_id",
            "organization_id",
            "release_id",
            "type",
            "level",
            "title",
            "transaction",
            "data",
            "tags",
            "hashes",
        }
        self.assertEqual(set(ISSUE_EVENT_EXPORT_COLUMN_TYPES.keys()), expected_columns)


class IssueEventRowTestCase(TestCase):
    """Test IssueEventRow dataclass and its properties."""

    def test_eventID_with_event_id(self):
        import uuid

        eid = uuid.uuid4()
        row = IssueEventRow(
            id=uuid.uuid4(),
            event_id=eid,
            timestamp=timezone.now(),
            issue_id=1,
            organization_id=1,
            release_id=None,
            type=0,
            level=40,
            title="Test",
            transaction="",
            data={},
            tags={},
            hashes=[],
        )
        self.assertEqual(row.eventID, eid.hex)

    def test_eventID_without_event_id(self):
        import uuid

        sid = uuid.uuid4()
        row = IssueEventRow(
            id=sid,
            event_id=None,
            timestamp=timezone.now(),
            issue_id=1,
            organization_id=1,
            release_id=None,
            type=0,
            level=40,
            title="Test",
            transaction="",
            data={},
            tags={},
            hashes=[],
        )
        self.assertEqual(row.eventID, sid.hex)

    def test_message_from_data(self):
        row = IssueEventRow(
            id=__import__("uuid").uuid4(),
            event_id=None,
            timestamp=timezone.now(),
            issue_id=1,
            organization_id=1,
            release_id=None,
            type=0,
            level=40,
            title="Fallback Title",
            transaction="",
            data={"message": "Custom message"},
            tags={},
            hashes=[],
        )
        self.assertEqual(row.message, "Custom message")

    def test_message_fallback_to_title(self):
        row = IssueEventRow(
            id=__import__("uuid").uuid4(),
            event_id=None,
            timestamp=timezone.now(),
            issue_id=1,
            organization_id=1,
            release_id=None,
            type=0,
            level=40,
            title="Error Title",
            transaction="",
            data={},
            tags={},
            hashes=[],
        )
        self.assertEqual(row.message, "Error Title")


class NoDuckDBTestCase(TestCase):
    """Tests that verify graceful behavior when DuckDB cold storage is disabled."""

    def test_cold_storage_returns_empty_when_disabled(self):
        from datetime import datetime
        from datetime import timezone as dt_timezone

        now = datetime.now(dt_timezone.utc)
        start = now - timedelta(days=1)

        results = query_cold_events(
            organization_id=1,
            start_dt=start,
            end_dt=now,
        )
        self.assertEqual(results, [])


class MaintenanceTestCase(TestCase):
    """Test cleanup_old_issue_events function."""

    def test_cleanup_noop_without_duckdb(self):
        """cleanup_old_issue_events should be a no-op when DuckDB is unavailable."""
        from ..maintenance import cleanup_old_issue_events

        # Should not raise
        cleanup_old_issue_events()

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="false")
    def test_cleanup_skips_when_disabled(self):
        from ..maintenance import cleanup_old_issue_events

        # Should not raise
        cleanup_old_issue_events()


class MaintainPartitionsSkipTestCase(TestCase):
    """Test that maintain_partitions skips issue_events when DuckDB is available."""

    @override_settings(GLITCHTIP_ENABLE_DUCKDB="true")
    def test_skip_issue_events_when_duckdb_available(self):
        """When DuckDB is available, issue_events should be skipped from standard drop."""
        self.assertTrue(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB=None,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_no_skip_without_duckdb(self):
        """When DuckDB is not available, standard drop should proceed."""
        self.assertFalse(is_duckdb_available())


class EventsHotDaysSettingTestCase(TestCase):
    """Test GLITCHTIP_EVENT_HOT_DAYS setting."""

    def test_default_value(self):
        hot_days = settings.GLITCHTIP_EVENT_HOT_DAYS
        self.assertEqual(hot_days, 30)


class MissingParquetTestCase(TestCase):
    """
    Test graceful handling when parquet files don't exist for issue events.

    Cold storage queries should return empty results, not raise exceptions.
    """

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
    )
    def test_query_cold_events_returns_empty(self):
        """query_cold_events returns [] when no parquet files exist."""
        with tempfile.TemporaryDirectory(prefix="glitchtip_cold_test_") as cold_dir:
            with self.settings(GLITCHTIP_COLD_STORAGE_DIR=cold_dir):
                results = query_cold_events(
                    organization_id=99999,
                    start_dt=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
                    end_dt=datetime(2020, 12, 31, tzinfo=dt_timezone.utc),
                )
                self.assertEqual(results, [])

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
    )
    def test_get_event_from_cold_returns_none(self):
        """get_event_from_cold returns None when parquet file doesn't exist."""
        with tempfile.TemporaryDirectory(prefix="glitchtip_cold_test_") as cold_dir:
            with self.settings(GLITCHTIP_COLD_STORAGE_DIR=cold_dir):
                fake_time = datetime(2020, 6, 15, tzinfo=dt_timezone.utc)
                fake_id = UUID7Helper.from_datetime(fake_time)
                result = get_event_from_cold(99999, fake_id, fake_time)
                self.assertIsNone(result)
