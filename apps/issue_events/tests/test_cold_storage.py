"""
Tests for issue event cold storage functionality with standalone DuckDB.

DuckDB runs in-process (no PostgreSQL extension required).
Tests that need S3 access are skipped when no bucket is configured.
"""

import shutil
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from glitchtip.cold_storage import (
    get_org_cold_storage_path,
    is_duckdb_available,
)
from glitchtip.partition_manager import PartitionManager, UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..cold_storage import (
    ISSUE_EVENT_EXPORT_COLUMN_TYPES,
    ISSUE_EVENT_SELECT_SQL,
    TABLE_NAME,
    IssueEventRow,
    get_event_from_cold,
    query_cold_events,
)


class DuckDBAvailabilityTestCase(TestCase):
    """Test DuckDB availability check."""

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true", GLITCHTIP_COLD_STORAGE_DIR="/tmp/cold"
    )
    def test_enabled_via_override(self):
        self.assertTrue(is_duckdb_available())

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        GLITCHTIP_COLD_STORAGE_DIR=None,
    )
    def test_enabled_but_no_storage_backend(self):
        """ENABLE_DUCKDB=true without a storage backend returns False."""
        self.assertFalse(is_duckdb_available())

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

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true", GLITCHTIP_COLD_STORAGE_DIR="/tmp/cold"
    )
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


def _create_issue_event_partition(date: datetime) -> str:
    """Create an issue event partition for a specific date. Returns partition name."""
    manager = PartitionManager()
    date_str = date.strftime("%Y%m%d")
    partition_name = f"issue_events_issueevent_{date_str}"
    next_date = date + timedelta(days=1)

    sqls = manager.create_time_partition(
        parent_table="issue_events_issueevent",
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


def _drop_issue_event_partition(partition_name: str):
    """Detach and drop a partition, ignoring errors."""
    with connection.cursor() as cursor:
        try:
            cursor.execute(
                f"ALTER TABLE issue_events_issueevent DETACH PARTITION {partition_name}"
            )
        except Exception:
            connection.connection.rollback()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {partition_name} CASCADE")
        except Exception:
            connection.connection.rollback()


class ArchiveThenQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """
    End-to-end test: insert issue events into PG, archive to Parquet,
    then query cold storage and verify results.
    """

    def setUp(self):
        self.create_project()
        self.cold_dir = tempfile.mkdtemp(prefix="glitchtip_cold_test_")
        self.archive_date = datetime(2025, 5, 1, tzinfo=dt_timezone.utc)
        self.partition_name = _create_issue_event_partition(self.archive_date)

    def tearDown(self):
        _drop_issue_event_partition(self.partition_name)
        shutil.rmtree(self.cold_dir, ignore_errors=True)

    def _insert_events(self, count: int) -> list:
        """Insert issue events into the partition and return their UUIDs."""
        from ..models import Issue

        issue = Issue.objects.create(
            project=self.project,
            title="Test Issue",
            metadata={"title": "Test Issue"},
            type=0,
            level=40,
        )

        event_ids = []
        with connection.cursor() as cursor:
            for i in range(count):
                event_time = self.archive_date + timedelta(seconds=i)
                event_id = UUID7Helper.from_datetime(event_time)
                event_ids.append(event_id)
                cursor.execute(
                    "INSERT INTO issue_events_issueevent "
                    "(id, timestamp, issue_id, organization_id, type, level, "
                    "title, transaction, data, tags, hashes) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        str(event_id),
                        event_time,
                        issue.id,
                        self.organization.id,
                        0,  # type
                        4,  # level (error = 4 in issue_events.constants.LogLevel)
                        f"Event {i}",
                        "/api/test",
                        "{}",
                        "{}",
                        "{}",
                    ],
                )
        return event_ids

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_archive_then_query_events(self):
        """Archived issue events are queryable from cold storage."""
        from glitchtip.cold_storage import archive_and_swap_partition

        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            event_ids = self._insert_events(20)

            archive_and_swap_partition(
                self.partition_name,
                TABLE_NAME,
                ISSUE_EVENT_EXPORT_COLUMN_TYPES,
                ISSUE_EVENT_SELECT_SQL,
            )

            results = query_cold_events(
                organization_id=self.organization.id,
                start_dt=self.archive_date,
                end_dt=self.archive_date + timedelta(days=1),
                limit=100,
            )

            self.assertEqual(len(results), 20)

            # Results should be sorted by id DESC
            result_ids = [r.id for r in results]
            self.assertEqual(result_ids, sorted(result_ids, reverse=True))

            # All original IDs should be present
            self.assertEqual(set(result_ids), set(event_ids))

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_archive_events_with_json_quotes(self):
        """Events with JSON containing embedded quotes archive correctly.

        Reproduces CSV parse errors seen in prod where data::text contains
        backslash-escaped quotes that conflict with standard CSV quoting.
        """
        import json

        from glitchtip.cold_storage import archive_and_swap_partition

        from ..models import Issue

        issue = Issue.objects.create(
            project=self.project,
            title='Error: ("Connection broken")',
            metadata={"title": 'Error: ("Connection broken")'},
            type=0,
            level=40,
        )

        event_time = self.archive_date + timedelta(seconds=1)
        event_id = UUID7Helper.from_datetime(event_time)
        # JSON data with nested quotes — the pattern that broke CSV parsing
        data = json.dumps(
            {
                "sdk": {"name": "sentry.python", "version": "1.5.4"},
                "message": 'ChunkedEncodingError: ("Connection broken: '
                "InvalidChunkLength(got length b'', 0 bytes read)\", "
                "InvalidChunkLength(got length b'', 0 bytes read))",
                "extra": {"sys.argv": ["scripts/report.py"]},
            }
        )
        tags = json.dumps({"browser": 'Chrome "Dev"', "os": "Linux"})

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO issue_events_issueevent "
                "(id, timestamp, issue_id, organization_id, type, level, "
                "title, transaction, data, tags, hashes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    str(event_id),
                    event_time,
                    issue.id,
                    self.organization.id,
                    0,
                    4,
                    'Error: ("Connection broken")',
                    "/api/test",
                    data,
                    tags,
                    "{}",
                ],
            )

        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            archive_and_swap_partition(
                self.partition_name,
                TABLE_NAME,
                ISSUE_EVENT_EXPORT_COLUMN_TYPES,
                ISSUE_EVENT_SELECT_SQL,
            )

            target_time = UUID7Helper.extract_datetime(event_id)
            result = get_event_from_cold(
                self.organization.id, event_id, target_time
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.id, event_id)
            self.assertIn("ChunkedEncodingError", result.data["message"])
            self.assertEqual(result.tags["browser"], 'Chrome "Dev"')

    @override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
    )
    def test_get_single_event_from_cold(self):
        """get_event_from_cold retrieves a specific event by ID."""
        from glitchtip.cold_storage import archive_and_swap_partition

        with self.settings(GLITCHTIP_COLD_STORAGE_DIR=self.cold_dir):
            event_ids = self._insert_events(5)

            archive_and_swap_partition(
                self.partition_name,
                TABLE_NAME,
                ISSUE_EVENT_EXPORT_COLUMN_TYPES,
                ISSUE_EVENT_SELECT_SQL,
            )

            target_id = event_ids[2]
            target_time = UUID7Helper.extract_datetime(target_id)

            result = get_event_from_cold(self.organization.id, target_id, target_time)

            self.assertIsNotNone(result)
            self.assertEqual(result.id, target_id)
            self.assertEqual(result.title, "Event 2")
            self.assertEqual(result.organization_id, self.organization.id)
