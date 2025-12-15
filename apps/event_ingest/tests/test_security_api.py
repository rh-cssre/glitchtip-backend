from django.urls import reverse

from apps.issue_events.models import Issue, IssueEvent

from .utils import EventIngestTestCase


class SecurityAPITestCase(EventIngestTestCase):
    """
    These test specifically test the security API and act more of integration test
    Use test_process_issue_events.py for testing Event Ingest more specifically
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_security", args=[self.project.id]) + self.params
        self.small_event = self.get_json_data(
            "apps/event_ingest/tests/test_data/csp/mozilla_example.json"
        )

    def test_security_api(self):
        with self.assertNumQueries(10):
            res = self.client.post(
                self.url, self.small_event, content_type="application/json"
            )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_csp_event(self):
        self.client.post(self.url, self.small_event, content_type="application/json")
        issue = Issue.objects.get()
        self.assertEqual(issue.title, "Blocked 'style-elem' from 'example.com'")
        event = IssueEvent.objects.get()
        self.assertEqual(event.data["csp"]["effective_directive"], "style-src-elem")
