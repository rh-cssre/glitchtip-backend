import json

from django.urls import reverse

from apps.issue_events.models import IssueEvent

from .utils import EventIngestTestCase


class EnvelopeAPITestCase(EventIngestTestCase):
    """
    These test specifically test the envelope API and act more of integration test
    Use test_process_issue_events.py for testing Event Ingest more specifically
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_envelope", args=[self.project.id]) + self.params
        self.django_event = self.get_json_data(
            "apps/event_ingest/tests/test_data/envelopes/django_message.json"
        )
        self.js_event = self.get_json_data(
            "apps/event_ingest/tests/test_data/envelopes/js_angular_message.json"
        )

    def get_string_payload(self, json_data):
        """Convert JSON data into envelope format string"""
        return "\n".join([json.dumps(line) for line in json_data])

    def test_envelope_api(self):
        with self.assertNumQueries(16):
            res = self.client.post(
                self.url, self.django_event, content_type="application/json"
            )
        self.assertContains(res, self.django_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_envelope_api_content_type(self):
        js_payload = self.get_string_payload(self.js_event)

        res = self.client.post(
            self.url, js_payload, content_type="text/plain;charset=UTF-8"
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.js_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)
