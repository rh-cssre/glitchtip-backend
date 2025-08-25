import uuid

from django.urls import reverse
from model_bakery import baker

from apps.issue_events.models import UserReport

from .utils import EventIngestTestCase


class ErrorPageEmbedTestCase(EventIngestTestCase):
    def setUp(self):
        self.url = reverse("api:get_embed_error_page")
        self.project = baker.make("projects.Project")
        self.project_key = baker.make("projects.ProjectKey", project=self.project)

    def test_get_not_found(self):
        res = self.client.get(self.url)
        # Slight deviation from OSS Sentry as it would 404
        self.assertEqual(res.status_code, 401)
        params = {"dsn": "lol", "eventId": uuid.uuid4().hex}
        res = self.client.get(self.url, params)
        self.assertEqual(res.status_code, 401)

    def test_get_embed(self):
        params = {"dsn": self.project_key.get_dsn(), "eventId": uuid.uuid4().hex}
        res = self.client.get(self.url, params)
        self.assertContains(res, self.project_key.public_key.hex)

    def test_get_embed_throttle(self):
        params = {"dsn": self.project_key.get_dsn(), "eventId": uuid.uuid4().hex}
        self.project.organization.event_throttle_rate = 100
        self.project.organization.save()
        res = self.client.get(self.url, params)
        self.assertEqual(res.status_code, 401)

        self.project.organization.event_throttle_rate = 50
        self.project.organization.save()
        res = self.client.get(self.url, params)
        self.assertContains(res, self.project_key.public_key.hex)

    def test_submit_report(self):
        params = f"?dsn={self.project_key.get_dsn()}&eventId={uuid.uuid4().hex}"
        data = {"name": "Test Name", "email": "test@example.com", "comments": "hmm"}
        res = self.client.post(self.url + params, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(UserReport.objects.filter(project=self.project).exists())

    def test_submit_report_with_issue(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        event = baker.make("issue_events.IssueEvent", issue=issue)
        params = f"?dsn={self.project_key.get_dsn()}&eventId={event.id.hex}"
        data = {"name": "Test Name", "email": "test@example.com", "comments": "hmm"}
        res = self.client.post(self.url + params, data)
        self.assertEqual(res.status_code, 200)
        created_report = UserReport.objects.filter(issue=issue).first()
        self.assertEqual(created_report.comments, data["comments"])
        self.assertEqual(created_report.name, data["name"])
