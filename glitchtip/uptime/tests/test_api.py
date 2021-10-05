from django.shortcuts import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class UptimeAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_list(self):
        url = reverse(
            "organization-monitors-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        monitor = baker.make(
            "uptime.Monitor", organization=self.organization, url="http://example.com"
        )
        res = self.client.get(url)
        self.assertContains(res, monitor.name)
