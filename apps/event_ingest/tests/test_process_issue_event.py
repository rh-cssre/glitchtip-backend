from .utils import EventIngestTestCase
from ..schema import InterchangeIssueEvent, IssueEventSchema
from ..process_event import process_issue_events
from apps.issue_events.models import Issue, IssueEvent, IssueHash


class IssueEventIngestTestCase(EventIngestTestCase):
    """
    These tests bypass the API and celery. They test the event ingest logic itself.
    This file should be large are test the following use cases
    - Multiple event saved at the same time
    - Sentry API compatibility
    - Default, Error, and CSP types
    - Graceful failure such as duplicate event ids or invalid data
    """

    def test_two_events(self):
        events = []
        for _ in range(2):
            payload = IssueEventSchema()
            events.append(
                InterchangeIssueEvent(project_id=self.project.id, payload=payload)
            )
        with self.assertNumQueries(12):
            process_issue_events(events)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueHash.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 2)
