import json
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip.test_utils import generators


class IssueStoreTestCase(APITestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")
        self.projectkey = self.project.projectkey_set.first()
        self.url = (
            f"/api/{self.project.id}/store/?sentry_key={self.projectkey.public_key}"
        )

    def test_store_api(self):
        with open("issues/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_store_api_auth_failure(self):
        url = "/api/1/store/"
        with open("issues/test_data/py_hi_event.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 403)

    def test_error_event(self):
        with open("issues/test_data/py_error.json") as json_file:
            data = json.load(json_file)
        res = self.client.post(self.url, data, format="json")

    def test_default_event(self):
        pass


class EventTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)

    def test_project_events(self):
        project = baker.make("projects.Project")
        event = baker.make("issues.Event", issue__project=project)
        url = f"/api/0/projects/{project.organization.slug}/{project.slug}/events/"
        res = self.client.get(url)
        self.assertContains(res, event.pk.hex)
