import json
from rest_framework.test import APITestCase


class IssueTestCase(APITestCase):
    def test_store_api(self):
        url = "/api/123/store/"
        with open("issues/test_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 201)
