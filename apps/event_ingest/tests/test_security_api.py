from django.shortcuts import reverse

from .utils import EventIngestTestCase
from apps.issue_events.models import IssueEvent


class SecurityAPITestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_security", args=[self.project.id]) + self.params
        self.small_event = self.get_event_json(
            "apps/event_ingest/tests/test_data/csp/mozilla_example.json"
        )

    def test_envelope_api(self):
        with self.assertNumQueries(7):
            res = self.client.post(
                self.url, self.small_event, content_type="application/json"
            )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)
