import json
import base64
from rest_framework.test import APITestCase
from model_bakery import baker


class IssueTestCase(APITestCase):
    def test_store_api(self):
        project = baker.make("projects.Project")
        projectkey = project.projectkey_set.first()

        url = f"/api/{project.id}/store/?sentry_key={projectkey.public_key}"
        with open("issues/test_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_store_api_auth_failure(self):
        url = "/api/1/store/"
        with open("issues/test_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 403)
