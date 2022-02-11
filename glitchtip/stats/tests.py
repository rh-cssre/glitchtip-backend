from django.shortcuts import reverse
from django.utils import timezone
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class StatsV2APITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("stats-v2", kwargs={"organization_slug": self.organization})

    def test_get(self):
        baker.make("events.Event")
        start = timezone.now() - timezone.timedelta(hours=2)
        end = timezone.now()
        res = self.client.get(
            self.url, {"category": "error", "start": start, "end": end}
        )
        self.assertEqual(res.status_code, 200)
