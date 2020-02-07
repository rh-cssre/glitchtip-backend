import json
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip.test_utils import generators
from issues.models import Issue, Event
from .test_data.csp import mdn_sample_csp


class EventStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_store", args=[self.project.id]) + self.params

    def test_store_api(self):
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        # Not implemented due to default serializer
        # res = self.client.post(self.url, data, format="json")
        # self.assertEqual(res.status_code, 200)

    def test_store_api_auth_failure(self):
        url = "/api/1/store/"
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 403)

    def test_error_event(self):
        with open("event_store/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")

    def test_csp_event(self):
        url = reverse("csp_store", args=[self.project.id]) + self.params
        data = mdn_sample_csp
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 200)
        expected_title = "Blocked 'style' from 'example.com'"
        issue = Issue.objects.get(title=expected_title)
        event = Event.objects.get()
        self.assertEqual(event.data["csp"]["effective_directive"], "style-src")
