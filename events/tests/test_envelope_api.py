import json
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from performance.models import TransactionEvent
from ..models import Event


class EnvelopeStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("envelope_store", args=[self.project.id]) + self.params

    def get_payload(self, path):
        """ Convert JSON file into envelope format string """
        with open(path) as json_file:
            json_data = json.load(json_file)
            data = "\n".join([json.dumps(line) for line in json_data])
        return data

    def test_accept(self):
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(TransactionEvent.objects.exists())

    def test_android_sdk_event(self):
        data = self.get_payload(
            "events/test_data/incoming_events/android_sdk_envelope.json"
        )
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.exists())
