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
        baker.make(
            "uptime.MonitorCheck", monitor=monitor, is_up=False, start_check="2021-09-19T15:39:31Z"
        )
        baker.make(
            "uptime.MonitorCheck", monitor=monitor, is_up=True, start_check="2021-09-19T15:40:31Z"
        )
        res = self.client.get(url)
        self.assertContains(res, monitor.name)
        # These tests below should probably be moved to the detail api 
        # endpoint once we create it
        self.assertEqual(res.data[0]["is_up"], True)
        self.assertEqual(res.data[0]["last_change"], "2021-09-19T15:39:31Z")
