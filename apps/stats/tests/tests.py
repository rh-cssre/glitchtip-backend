from datetime import datetime

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


class StatsV2APITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("api:stats_v2", args=[self.organization.slug])

    def test_get(self):
        baker.make("issue_events.IssueEvent", issue__project=self.project)
        start = timezone.now() - timezone.timedelta(hours=2)
        end = timezone.now()
        res = self.client.get(
            self.url,
            {"category": "error", "start": start, "end": end, "field": "sum(quantity)"},
        )
        self.assertEqual(res.status_code, 200)

        response = res.json()
        self.assertIsInstance(response["intervals"], list)
        self.assertEqual(len(response["intervals"]), 4)
        self.assertIsInstance(
            datetime.fromisoformat(response["intervals"][0]), datetime
        )
