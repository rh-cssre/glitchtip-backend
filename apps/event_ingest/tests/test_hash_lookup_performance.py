"""
Tests proving the performance characteristics of the hash dict lookup
and primary fallback optimizations in process_issue_events.

Key insight: ingest tasks run in batches. The dict lookup resolves
existing issues in O(1) per event with constant query count regardless
of batch size. The primary fallback prevents IntegrityErrors caused by
replication lag.
"""

import uuid
from unittest.mock import patch

from django.db import connections
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent, IssueHash, IssueIndex
from glitchtip.async_compat import async_connections

from ..process_event import process_issue_events
from ..schema import IssueEventSchema, IssueTaskMessage
from .utils import EventIngestTestCase, generate_event, run_async_closing


def _process_issue_events(*args, **kwargs):
    return run_async_closing(process_issue_events, *args, **kwargs)


class HashLookupBatchTestCase(EventIngestTestCase):
    """
    Prove that query count stays bounded as batch size grows
    when all events match existing issues.
    """

    def _make_batch(self, messages, count):
        """Generate a batch of count events cycling through messages."""
        return [
            generate_event(
                event={
                    "message": messages[i % len(messages)],
                    "event_id": uuid.uuid4(),
                }
            )
            for i in range(count)
        ]

    def test_batch_existing_issues_constant_query_count(self):
        """
        Process batches of 10 and 30 events, all matching existing issues.
        Query count should be identical — the dict lookup keeps it constant.
        """
        # Seed 5 distinct issues
        seed_msgs = [f"error-type-{i}" for i in range(5)]
        self.process_events([generate_event(event={"message": m}) for m in seed_msgs])
        self.assertEqual(Issue.objects.count(), 5)

        # Batch of 10
        with CaptureQueriesContext(connections["default"]) as ctx_10:
            self.process_events(self._make_batch(seed_msgs, 10))

        # Batch of 30 — 3x larger
        with CaptureQueriesContext(connections["default"]) as ctx_30:
            self.process_events(self._make_batch(seed_msgs, 30))

        self.assertEqual(
            len(ctx_10),
            len(ctx_30),
            f"Query count should be constant regardless of batch size. "
            f"10 events: {len(ctx_10)} queries, 30 events: {len(ctx_30)} queries",
        )
        # No new issues created
        self.assertEqual(Issue.objects.count(), 5)

    def test_new_issues_cost_more_queries_than_existing(self):
        """
        Creating new issues (INSERT path) costs more queries per event
        than matching existing issues (dict lookup path). This is the
        overhead the primary fallback helps avoid.
        """
        # Seed issues
        seed_msgs = [f"seed-error-{i}" for i in range(5)]
        self.process_events([generate_event(event={"message": m}) for m in seed_msgs])

        # Batch matching existing issues
        with CaptureQueriesContext(connections["default"]) as ctx_existing:
            self.process_events(self._make_batch(seed_msgs, 5))

        # Batch creating new issues
        new_msgs = [f"brand-new-error-{i}" for i in range(5)]
        with CaptureQueriesContext(connections["default"]) as ctx_new:
            self.process_events(self._make_batch(new_msgs, 5))

        self.assertGreater(
            len(ctx_new),
            len(ctx_existing),
            f"INSERT path should cost more queries than dict-lookup path. "
            f"New: {len(ctx_new)}, Existing: {len(ctx_existing)}",
        )


class PrimaryFallbackTestCase(EventIngestTestCase):
    """
    Test the read_only_db != "default" fallback branch.

    Mocks connections so "read_only" routes to "default" (no real second
    DB needed), and mocks IssueHash's initial query to return empty
    (simulating a stale replica). The fallback queries .using("default")
    and finds the real data.
    """

    def _process_with_stale_replica(self, data):
        """
        Call process_issue_events with read_only_db="read_only".
        Routes "read_only" connections to "default" so Project/other
        queries work, but returns empty for the IssueHash initial query
        to trigger the primary fallback.
        """
        if isinstance(data, dict):
            data = [data]
        events = [
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id if self.organization else None,
                received=timezone.now(),
                payload=IssueEventSchema(**d),
            )
            for d in data
        ]

        original_getitem = type(connections).__getitem__
        original_async_getitem = type(async_connections).__getitem__
        original_ih_using = IssueHash.objects.using

        def mock_getitem(self_conn, alias):
            if alias == "read_only":
                return original_getitem(self_conn, "default")
            return original_getitem(self_conn, alias)

        def mock_async_getitem(self_conn, alias):
            if alias == "read_only":
                return original_async_getitem(self_conn, "default")
            return original_async_getitem(self_conn, alias)

        def mock_ih_using(alias):
            if alias == "read_only":
                return original_ih_using("default").none()
            return original_ih_using(alias)

        with (
            patch.object(type(connections), "__getitem__", mock_getitem),
            patch.object(type(async_connections), "__getitem__", mock_async_getitem),
            patch.object(IssueHash.objects, "using", mock_ih_using),
        ):
            _process_issue_events(events, read_only_db="read_only")
        return events

    def test_fallback_finds_existing_issue(self):
        """
        Replica returns empty for IssueHash (mocked), fallback queries
        primary and finds the hash. Event should match the existing
        issue — no duplicate created.
        """
        self.process_events(generate_event())
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

        self._process_with_stale_replica(
            generate_event(event={"event_id": uuid.uuid4()})
        )
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 2)

    def test_fallback_creates_new_issue(self):
        """
        When no matching issue exists, the fallback query finds nothing
        and the INSERT path creates the issue normally.
        """
        self._process_with_stale_replica(generate_event())
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueHash.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_fallback_reopens_resolved_issue(self):
        """
        A resolved issue found via the primary fallback should be reopened.
        """
        self.process_events(generate_event())
        issue = Issue.objects.first()
        IssueIndex.objects.filter(issue=issue).update(status=EventStatus.RESOLVED)

        self._process_with_stale_replica(
            generate_event(event={"event_id": uuid.uuid4()})
        )
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)
