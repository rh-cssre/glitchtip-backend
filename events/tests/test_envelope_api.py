import json
import uuid

from django.shortcuts import reverse
from django.test import override_settings
from model_bakery import baker
from rest_framework.test import APITestCase

from glitchtip import test_utils  # pylint: disable=unused-import
from performance.models import TransactionEvent, TransactionGroup

from ..models import Event


class EnvelopeStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("envelope_store", args=[self.project.id]) + self.params

    def get_payload(self, path, replace_id=False, set_release=None):
        """Convert JSON file into envelope format string"""
        with open(path) as json_file:
            json_data = json.load(json_file)
            if replace_id:
                new_id = uuid.uuid4().hex
                json_data[0]["event_id"] = new_id
                json_data[2]["event_id"] = new_id
            if set_release:
                json_data[0]["trace"]["release"] = set_release
                json_data[2]["release"] = set_release
            data = "\n".join([json.dumps(line) for line in json_data])
        return data

    def test_accept(self):
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(TransactionEvent.objects.exists())

    def test_maintenance_freeze(self):
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        with override_settings(MAINTENANCE_EVENT_FREEZE=True):
            res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 503)

    def test_accept_js_transaction(self):
        data = self.get_payload("events/test_data/transactions/js_simple.json")
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(TransactionEvent.objects.exists())

    def test_accept_dsn_event(self):
        # Ensure JS tunnel works
        # https://gitlab.com/glitchtip/glitchtip-backend/-/issues/181
        data = [
            {
                "event_id": "37f658fddae1465ab1ed7569ca653177",
                "dsn": f"http://{self.projectkey.public_key}@172.17.0.1:8000/18",
            },
            {"type": "event"},
            {"exception": {"values": []}},
        ]
        data = "\n".join([json.dumps(line) for line in data])
        res = self.client.generic(
            "POST", reverse("envelope_store", args=[self.project.id]), data
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.exists())

    def test_android_sdk_event(self):
        data = self.get_payload(
            "events/test_data/incoming_events/android_sdk_envelope.json"
        )
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Event.objects.exists())

    def test_js_angular(self):
        data = self.get_payload("events/test_data/transactions/js_angular.json")
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 200)

    def test_environment_release(self):
        data = self.get_payload(
            "events/test_data/transactions/environment_release.json"
        )
        res = self.client.generic("POST", self.url, data)
        event_id = res.data["id"]
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            TransactionEvent.objects.filter(
                pk=event_id, tags__release="1.0", tags__environment="dev"
            ).exists()
        )
        self.assertTrue(
            TransactionGroup.objects.filter(
                transactionevent__pk=event_id,
                tags__release__contains="1.0",
                tags__environment__contains="dev",
            ).exists()
        )

        data = self.get_payload(
            "events/test_data/transactions/environment_release.json",
            replace_id=True,
            set_release="1.1",
        )
        res = self.client.generic("POST", self.url, data)
        self.assertTrue(
            TransactionGroup.objects.filter(
                transactionevent__pk=event_id,
                tags__release__contains="1.1",
                tags__environment__contains="dev",
            ).exists()
        )

    def test_duplicate_id(self):
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        self.client.generic("POST", self.url, data)
        res = self.client.generic("POST", self.url, data)
        self.assertEqual(res.status_code, 400)
