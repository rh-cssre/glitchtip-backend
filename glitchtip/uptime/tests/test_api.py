from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase
from glitchtip.uptime.models import Monitor


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
        self.assertEqual(res.data[0]["isUp"], True)
        self.assertEqual(res.data[0]["lastChange"], "2021-09-19T15:39:31Z")

    def test_create(self):
        url = reverse(
            "organization-monitors-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        data = {
            "monitorType": "ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "interval": "00:01:00",
            "project": self.project.pk
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.monitor_type, data["monitorType"])
        self.assertEqual(monitor.organization, self.organization)
        self.assertEqual(monitor.project, self.project)
