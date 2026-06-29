"""
Integration tests for Storage Engine V2 (dual-ID schema with UUIDv7 partitioning).

Tests the new IssueEvent model with:
- Server-generated UUIDv7 IDs (partition key)
- Client-provided UUIDv4 event_ids (nullable)
- Smart lookup via EventManager
- Partition targeting for performance
"""

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase, TransactionTestCase
from django.utils import timezone as django_timezone
from model_bakery import baker

from glitchtip.partition_manager import UUID7Helper

from ..models import IssueEvent


class UUID7EventCreationTestCase(TestCase):
    """Test that events are created with UUIDv7 IDs by default"""

    def test_event_created_with_uuid7(self):
        """New events should have server-generated UUIDv7 IDs"""
        issue = baker.make("issue_events.Issue")

        event = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test Event",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Verify ID is UUIDv7
        self.assertEqual(event.id.version, 7)
        self.assertIsNotNone(event.id)

        # Verify event_id is None (not provided by client)
        self.assertIsNone(event.event_id)

    def test_event_with_client_event_id(self):
        """Events can store client-provided event_id separately"""
        issue = baker.make("issue_events.Issue")
        client_uuid = uuid4()  # Simulate SDK-provided UUIDv4

        event = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            event_id=client_uuid,  # Client-provided
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test Event with Client ID",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Verify dual IDs
        self.assertEqual(event.id.version, 7)  # Server ID is v7
        self.assertEqual(event.event_id, client_uuid)  # Client ID preserved
        self.assertEqual(event.event_id.version, 4)  # Client ID is v4

    def test_eventID_property_prefers_event_id(self):
        """eventID property should return event_id if present, else id"""
        issue = baker.make("issue_events.Issue")
        client_uuid = uuid4()

        # Event WITH client event_id
        event_with_client_id = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            event_id=client_uuid,
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test",
            transaction="test",
            data={},
            tags={},
        )

        # Should return client event_id
        self.assertEqual(event_with_client_id.eventID, client_uuid.hex)

        # Event WITHOUT client event_id
        event_without_client_id = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test",
            transaction="test",
            data={},
            tags={},
        )

        # Should return server id
        self.assertEqual(
            event_without_client_id.eventID, event_without_client_id.id.hex
        )


class EventManagerLookupTestCase(TransactionTestCase):
    """Test smart UUID lookup with partition targeting"""

    def test_get_event_by_uuid7_id(self):
        """Lookup by UUIDv7 should use partition targeting"""
        issue = baker.make("issue_events.Issue")
        received_time = django_timezone.now()

        event = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            timestamp=received_time,
            type=0,
            level=4,
            title="Test Event",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Lookup by server ID (UUIDv7)
        found_event = IssueEvent.objects.get_event(event.id)
        self.assertEqual(found_event.id, event.id)
        self.assertEqual(found_event.title, "Test Event")

    def test_get_event_by_uuid4_event_id(self):
        """Lookup by UUIDv4 event_id should use partial index"""
        issue = baker.make("issue_events.Issue")
        client_uuid = uuid4()

        IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            event_id=client_uuid,
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test Event",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Lookup by client event_id (UUIDv4)
        found_event = IssueEvent.objects.get_event(client_uuid)
        self.assertEqual(found_event.event_id, client_uuid)
        self.assertEqual(found_event.title, "Test Event")

    def test_get_event_by_string_uuid(self):
        """get_event should accept UUID strings"""
        issue = baker.make("issue_events.Issue")

        event = IssueEvent.objects.create(
            issue=issue,
            organization=issue.project.organization,
            timestamp=django_timezone.now(),
            type=0,
            level=4,
            title="Test Event",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Lookup by string representation
        found_event = IssueEvent.objects.get_event(str(event.id))
        self.assertEqual(found_event.id, event.id)

    def test_get_event_invalid_uuid(self):
        """Invalid UUID string should raise ValueError"""
        with self.assertRaises(ValueError):
            IssueEvent.objects.get_event("not-a-valid-uuid")

    def test_get_event_not_found(self):
        """Non-existent UUID should raise DoesNotExist"""
        random_uuid = uuid4()

        with self.assertRaises(IssueEvent.DoesNotExist):
            IssueEvent.objects.get_event(random_uuid)


class UUID7TimestampTestCase(TestCase):
    """Test UUIDv7 timestamp encoding/extraction"""

    def test_uuid7_contains_timestamp(self):
        """UUIDv7 ID should encode the received timestamp"""
        issue = baker.make("issue_events.Issue")
        received_time = django_timezone.now()

        event = IssueEvent.objects.create(
            id=UUID7Helper.from_datetime(received_time),
            issue=issue,
            organization=issue.project.organization,
            timestamp=received_time,
            type=0,
            level=4,
            title="Test Event",
            transaction="test.transaction",
            data={},
            tags={},
        )

        # Extract timestamp from UUIDv7
        extracted_time = UUID7Helper.extract_datetime(event.id)

        # Should match within millisecond precision
        delta = abs((extracted_time - received_time).total_seconds())
        self.assertLess(delta, 0.001)

    def test_uuid7_temporal_ordering(self):
        """UUIDv7s should maintain temporal ordering"""
        issue = baker.make("issue_events.Issue")
        base_time = django_timezone.now()

        # Create events at different times
        t1 = base_time
        event1 = IssueEvent.objects.create(
            id=UUID7Helper.from_datetime(t1),
            issue=issue,
            organization=issue.project.organization,
            timestamp=t1,
            type=0,
            level=4,
            title="Event 1",
            transaction="test",
            data={},
            tags={},
        )

        t2 = base_time + timedelta(seconds=1)
        event2 = IssueEvent.objects.create(
            id=UUID7Helper.from_datetime(t2),
            issue=issue,
            organization=issue.project.organization,
            timestamp=t2,
            type=0,
            level=4,
            title="Event 2",
            transaction="test",
            data={},
            tags={},
        )

        t3 = base_time + timedelta(seconds=2)
        event3 = IssueEvent.objects.create(
            id=UUID7Helper.from_datetime(t3),
            issue=issue,
            organization=issue.project.organization,
            timestamp=t3,
            type=0,
            level=4,
            title="Event 3",
            transaction="test",
            data={},
            tags={},
        )

        # UUIDs should be sortable by time
        self.assertLess(event1.id, event2.id)
        self.assertLess(event2.id, event3.id)
        self.assertLess(event1.id, event3.id)


class EventTimeRangeQueryTestCase(TransactionTestCase):
    """Test time-range queries with UUID partition targeting"""

    def test_filter_by_time_range(self):
        """Time-range queries should use UUID boundaries for partition pruning"""
        issue = baker.make("issue_events.Issue")
        base_time = django_timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Create events across multiple days
        for i in range(5):
            event_time = base_time + timedelta(days=i)
            IssueEvent.objects.create(
                id=UUID7Helper.from_datetime(event_time),
                issue=issue,
                organization=issue.project.organization,
                timestamp=event_time,
                type=0,
                level=4,
                title=f"Event Day {i}",
                transaction="test",
                data={},
                tags={},
            )

        # Query for events in days 1-3
        start = base_time + timedelta(days=1)
        end = base_time + timedelta(days=4)

        events = IssueEvent.objects.filter_by_time_range(start, end)

        # Should return 3 events (days 1, 2, 3)
        self.assertEqual(events.count(), 3)

        # Verify correct events returned
        titles = [e.title for e in events]
        self.assertIn("Event Day 1", titles)
        self.assertIn("Event Day 2", titles)
        self.assertIn("Event Day 3", titles)
        self.assertNotIn("Event Day 0", titles)
        self.assertNotIn("Event Day 4", titles)


class ColumnAlignmentTestCase(TestCase):
    """Verify column order follows alignment optimization"""

    def test_model_field_alignment(self):
        """
        Verify fields are ordered for optimal memory alignment:
        16-byte (UUID) -> 8-byte (timestamp, FK) -> 2-byte (smallint) -> variable
        """
        from django.db import connection

        # Query actual column order from PostgreSQL
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT column_name, ordinal_position, data_type
                FROM information_schema.columns
                WHERE table_name = 'issue_events_issueevent'
                ORDER BY ordinal_position;
            """)

            columns = cursor.fetchall()

        if not columns:
            # Table might not exist in test DB, skip
            self.skipTest("Table not created yet")

        column_names = [col[0] for col in columns]

        # Verify UUID fields come first (16-byte alignment)
        uuid_fields = [name for name in column_names if name in ["id", "event_id"]]
        self.assertTrue(
            all(column_names.index(f) < 3 for f in uuid_fields),
            "UUID fields should be at the beginning for 16-byte alignment",
        )

        # Verify timestamp fields come before smallint fields
        # Note: `received` is no longer stored - it's derived from UUIDv7 id
        timestamp_fields = ["timestamp"]
        smallint_fields = ["type", "level"]

        for ts_field in timestamp_fields:
            for si_field in smallint_fields:
                if ts_field in column_names and si_field in column_names:
                    self.assertLess(
                        column_names.index(ts_field),
                        column_names.index(si_field),
                        f"8-byte {ts_field} should come before 2-byte {si_field}",
                    )
