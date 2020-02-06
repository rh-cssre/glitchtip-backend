import json
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip.test_utils import generators


class EventStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.params = f"?sentry_key={self.projectkey.public_key}"
        self.url = reverse("event_store", args=[self.project.id]) + self.params

    def test_store_api(self):
        with open("event_store/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

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
        data = {
            "csp-report": {
                "document-uri": "http://example.com/signup.html",
                "referrer": "",
                "blocked-uri": "http://example.com/css/style.css",
                "violated-directive": "style-src cdn.example.com",
                "original-policy": "default-src 'none'; style-src cdn.example.com; report-uri /_/csp-reports",
            }
        }
        # res = self.client.post(url, data, format="json")
        # import ipdb
