"""
Tests for LogEvent model with UUIDv7 partitioning.
"""

from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone as django_timezone
from model_bakery import baker

from glitchtip.partition_manager import UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import LogLevel
from ..models import LogEvent


def make_project():
    """Helper to create a project with proper baker config."""
    return baker.make("projects.Project", organization__scrub_ip_addresses=False)


class LogEventCreationTestCase(TestCase):
    """Test that log events are created with UUIDv7 IDs by default"""

    def test_log_created_with_uuid7(self):
        """New logs should have server-generated UUIDv7 IDs"""
        project = make_project()
        now = django_timezone.now()

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log message",
        )

        # Verify ID is UUIDv7
        self.assertEqual(log.id.version, 7)
        self.assertIsNotNone(log.id)

    def test_log_with_trace_id(self):
        """Log events can store trace IDs for correlation"""
        project = make_project()
        now = django_timezone.now()
        trace_id = UUID7Helper.from_datetime()

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log with trace",
            trace_id=trace_id,
        )

        self.assertEqual(log.trace_id, trace_id)

    def test_log_levels(self):
        """Test all log levels can be stored"""
        project = make_project()

        for level in LogLevel:
            now = django_timezone.now()
            log = LogEvent.objects.create(
                id=UUID7Helper.from_datetime(now),
                organization=project.organization,
                project=project,
                level=level,
                body=f"Test {level.label} log",
            )
            self.assertEqual(log.level, level)
            self.assertEqual(log.get_level_display(), level.label)


class LogEventUUID7TimestampTestCase(TestCase):
    """Test UUIDv7 timestamp encoding/extraction for logs"""

    def test_uuid7_contains_timestamp(self):
        """UUIDv7 ID should encode the received timestamp"""
        project = make_project()
        received_time = django_timezone.now()

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(received_time),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log",
        )

        # Extract timestamp from UUIDv7
        extracted_time = UUID7Helper.extract_datetime(log.id)

        # Should match within millisecond precision
        delta = abs((extracted_time - received_time).total_seconds())
        self.assertLess(delta, 0.001)

    def test_timestamp_property(self):
        """The timestamp property should extract time from UUIDv7"""
        project = make_project()
        received_time = django_timezone.now()

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(received_time),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log",
        )

        # timestamp property should match the encoded time
        delta = abs((log.timestamp - received_time).total_seconds())
        self.assertLess(delta, 0.001)

    def test_uuid7_temporal_ordering(self):
        """UUIDv7s should maintain temporal ordering"""
        project = make_project()
        base_time = django_timezone.now()

        # Create logs at different times
        logs = []
        for i in range(3):
            t = base_time + timedelta(seconds=i)
            log = LogEvent.objects.create(
                id=UUID7Helper.from_datetime(t),
                organization=project.organization,
                project=project,
                level=LogLevel.INFO,
                body=f"Log {i}",
            )
            logs.append(log)

        # UUIDs should be sortable by time
        self.assertLess(logs[0].id, logs[1].id)
        self.assertLess(logs[1].id, logs[2].id)


class LogEventDataTestCase(TestCase):
    """Test LogEvent data field handling"""

    def test_data_field_stores_json(self):
        """The data field should store arbitrary JSON"""
        project = make_project()
        now = django_timezone.now()

        data = {
            "user_id": 123,
            "request_id": "abc-123",
            "metadata": {"key": "value"},
        }

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log",
            data=data,
        )

        # Reload from database
        log.refresh_from_db()
        self.assertEqual(log.data, data)

    def test_service_field(self):
        """Test service name storage"""
        project = make_project()
        now = django_timezone.now()

        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=project.organization,
            project=project,
            level=LogLevel.INFO,
            body="Test log",
            service="api-gateway",
        )

        self.assertEqual(log.service, "api-gateway")


class LogEventQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test log event queries"""

    def setUp(self):
        self.create_project()

    def test_filter_by_organization(self):
        """Logs should be filtered by organization"""
        project1 = self.project
        project2 = make_project()
        now = django_timezone.now()

        LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now),
            organization=project1.organization,
            project=project1,
            level=LogLevel.INFO,
            body="Log 1",
        )
        LogEvent.objects.create(
            id=UUID7Helper.from_datetime(now + timedelta(milliseconds=1)),
            organization=project2.organization,
            project=project2,
            level=LogLevel.INFO,
            body="Log 2",
        )

        logs1 = LogEvent.objects.filter(organization=project1.organization)
        logs2 = LogEvent.objects.filter(organization=project2.organization)

        self.assertEqual(logs1.count(), 1)
        self.assertEqual(logs2.count(), 1)
        self.assertEqual(logs1.first().body, "Log 1")
        self.assertEqual(logs2.first().body, "Log 2")

    def test_filter_by_level(self):
        """Logs should be filterable by level"""
        project = self.project
        now = django_timezone.now()

        for i, level in enumerate([LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR]):
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(now + timedelta(milliseconds=i)),
                organization=project.organization,
                project=project,
                level=level,
                body=f"Log {level.label}",
            )

        error_logs = LogEvent.objects.filter(
            organization=project.organization, level=LogLevel.ERROR
        )
        self.assertEqual(error_logs.count(), 1)

    def test_order_by_id_desc(self):
        """Logs should be orderable by id descending (most recent first)"""
        project = self.project
        base_time = django_timezone.now()

        for i in range(3):
            t = base_time + timedelta(seconds=i)
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(t),
                organization=project.organization,
                project=project,
                level=LogLevel.INFO,
                body=f"Log {i}",
            )

        logs = LogEvent.objects.filter(organization=project.organization).order_by(
            "-id"
        )

        # Most recent should be first
        self.assertEqual(logs[0].body, "Log 2")
        self.assertEqual(logs[1].body, "Log 1")
        self.assertEqual(logs[2].body, "Log 0")
