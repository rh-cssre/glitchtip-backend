from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase


class StatsAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_org_team_project()
        self.set_client_credentials(self.auth_token.token)
        self.event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        self.url = reverse("api:stats_v2", args=[self.organization.slug])

    def test_get(self):
        start = timezone.now() - timezone.timedelta(hours=1)
        end = timezone.now()
        query = {
            "category": "error",
            "start": start,
            "end": end,
            "field": "sum(quantity)",
        }
        res = self.client.get(self.url, query, **self.get_headers())
        self.assertEqual(res.status_code, 403)
        self.auth_token.add_permission("org:read")
        res = self.client.get(self.url, query, **self.get_headers())
        self.assertEqual(res.status_code, 200)

    def test_get_for_project(self):
        start = timezone.now() - timezone.timedelta(hours=1)
        end = timezone.now()
        query = {
            "category": "error",
            "start": start,
            "end": end,
            "field": "sum(quantity)",
            "project": [self.project.pk],
        }
        self.auth_token.add_permission("org:read")
        res = self.client.get(self.url, query, **self.get_headers())
        self.assertEqual(res.status_code, 200)

    def test_project_validation(self):
        start = timezone.now() - timezone.timedelta(hours=1)
        end = timezone.now()
        query = {
            "category": "error",
            "start": start,
            "end": end,
            "field": "sum(quantity)",
            "project": ["string"],
        }
        self.auth_token.add_permission("org:read")
        res = self.client.get(self.url, query, **self.get_headers())
        self.assertEqual(res.status_code, 422)
