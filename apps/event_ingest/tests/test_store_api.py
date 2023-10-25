import json

from django.test import override_settings
from django.shortcuts import reverse
from model_bakery import baker

from .utils import EventIngestTestCase
from apps.issue_events.models import IssueEvent


class StoreAPITestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_store", args=[self.project.id]) + self.params
        self.small_event = self.get_event_json("events/test_data/py_hi_event.json")

    def test_store_api(self):
        with self.assertNumQueries(7):
            res = self.client.post(
                self.url, self.small_event, content_type="application/json"
            )
        self.assertContains(res, self.small_event["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    @override_settings(MAINTENANCE_EVENT_FREEZE=True)
    def test_maintenance_freeze(self):
        res = self.client.post(
            self.url, self.small_event, content_type="application/json"
        )
        self.assertEqual(res.status_code, 503)
