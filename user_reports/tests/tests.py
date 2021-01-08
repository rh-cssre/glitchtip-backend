import uuid
from django.shortcuts import reverse
from model_bakery import baker
from rest_framework.test import APITestCase
from glitchtip import test_utils  # pylint: disable=unused-import
from ..models import UserReport


class ErrorPageEmbedTestCase(APITestCase):
    def setUp(self):
        self.url = reverse("error_page")
        self.project = baker.make("projects.Project")
        self.project_key = baker.make("projects.ProjectKey", project=self.project)

    def test_get_not_found(self):
        res = self.client.get(self.url)
        # Slight deviation from OSS Sentry as it would 404, but better consistency with DRF
        self.assertEqual(res.status_code, 400)
        res = self.client.get(self.url + "?dsn=lol")
        self.assertEqual(res.status_code, 400)

    def test_get_embed(self):
        params = {"dsn": self.project_key.get_dsn(), "eventId": uuid.uuid4().hex}
        res = self.client.get(self.url, params)
        self.assertContains(res, self.project_key.public_key.hex)

    def test_submit_report(self):
        params = f"?dsn={self.project_key.get_dsn()}&eventId={uuid.uuid4().hex}"
        data = {"name": "Test Name", "email": "test@example.com", "comments": "hmm"}
        res = self.client.post(self.url + params, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(UserReport.objects.filter(project=self.project).exists())

    def test_submit_report_with_issue(self):
        issue = baker.make("issues.Issue", project=self.project)
        event = baker.make("events.Event", issue=issue)
        params = f"?dsn={self.project_key.get_dsn()}&eventId={event.event_id.hex}"
        data = {"name": "Test Name", "email": "test@example.com", "comments": "hmm"}
        res = self.client.post(self.url + params, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(UserReport.objects.filter(issue=issue).exists())
