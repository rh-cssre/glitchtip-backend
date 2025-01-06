from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase


class StatsAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.event = baker.make(
            "issue_events.IssueEvent", issue__project__organization=self.organization
        )
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
