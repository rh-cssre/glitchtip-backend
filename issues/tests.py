import json
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip.test_utils import generators
from .models import Issue, EventStatus


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

    def test_events_latest(self):
        """
        Should show more recent event with previousEventID of previous/first event
        """
        event = baker.make("issues.Event")
        event2 = baker.make("issues.Event", issue=event.issue)
        url = f"/api/0/issues/{event.issue.id}/events/latest/"
        res = self.client.get(url)
        self.assertContains(res, event2.pk.hex)
        self.assertEqual(res.data["previousEventID"], event.pk.hex)
        self.assertEqual(res.data["nextEventID"], None)


class IssuesAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = "/api/0/issues/"

    def test_bulk_update(self):
        """ Bulk update only supports Issue status """
        project = baker.make("projects.Project")
        issues = baker.make(Issue, project=project, _quantity=2)
        url = f"{self.url}?id={issues[0].id}&id={issues[1].id}"
        status_to_set = EventStatus.RESOLVED
        data = {"status": status_to_set.label}
        res = self.client.put(url, data)
        self.assertContains(res, status_to_set.label)
        issues = Issue.objects.all()
        self.assertEqual(issues[0].status, status_to_set)
        self.assertEqual(issues[1].status, status_to_set)

    def test_filter_project(self):
        baker.make(Issue)
        project = baker.make("projects.Project")
        issue = baker.make(Issue, project=project)

        res = self.client.get(self.url, {"project": project.id})
        self.assertEqual(len(res.data), 1)
        self.assertContains(res, issue.id)

    def test_filter_is_status(self):
        """ Match sentry's usage of "is" for status filtering """
        resolved_issue = baker.make(Issue, status=EventStatus.RESOLVED)
        unresolved_issue = baker.make(Issue, status=EventStatus.UNRESOLVED)
        res = self.client.get(self.url, {"query": "is:unresolved has:platform"})
        self.assertEqual(len(res.data), 1)
        self.assertContains(res, unresolved_issue.id)
        self.assertNotContains(res, resolved_issue.id)

