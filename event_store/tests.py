import json
from rest_framework.test import APITestCase
from glitchtip.test_utils import generators
from model_bakery import baker


class EventStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.url = (
            f"/api/{self.project.id}/store/?sentry_key={self.projectkey.public_key}"
        )

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

    def test_default_event(self):
        pass

