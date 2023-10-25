import uuid

from django.shortcuts import reverse

from .utils import EventIngestTestCase


class EnvelopeAPITestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_envelope", args=[self.project.id]) + self.params
        self.small_event = self.get_event_json(
            "apps/event_ingest/tests/test_data/envelopes/django_message.json"
        )

    def test_envelope_api(self):
        with self.assertNumQueries(7):
            res = self.client.post(
                self.url, self.small_event, content_type="application/json"
            )
            print(res.json())
        self.assertContains(res, self.small_event["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)
