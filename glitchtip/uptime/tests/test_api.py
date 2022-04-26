from unittest import mock

from django.shortcuts import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase
from glitchtip.uptime.models import Monitor


class UptimeAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.list_url = reverse(
            "organization-monitors-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def test_list(self):
        monitor = baker.make(
            "uptime.Monitor", organization=self.organization, url="http://example.com"
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=True,
            start_check="2021-09-19T15:40:31Z",
        )
        res = self.client.get(self.list_url)
        self.assertContains(res, monitor.name)
        self.assertEqual(res.data[0]["isUp"], True)
        self.assertEqual(res.data[0]["lastChange"], "2021-09-19T15:39:31Z")

    def test_list_aggregation(self):
        """Test up and down event aggregations"""
        monitor = baker.make(
            "uptime.Monitor", organization=self.organization, url="http://example.com"
        )
        start_time = timezone.now()
        # Make 100 events, 50 up and then 50 up and down every minute
        for i in range(99):
            is_up = i % 2
            if i < 50:
                is_up = True
            current_time = start_time + timezone.timedelta(minutes=i)
            with freeze_time(current_time):
                baker.make(
                    "uptime.MonitorCheck",
                    monitor=monitor,
                    is_up=is_up,
                    start_check=current_time,
                )
        with freeze_time(current_time):
            res = self.client.get(self.list_url)
        self.assertEqual(len(res.data[0]["checks"]), 60)

    def test_create(self):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "interval": "00:01:00",
            "project": self.project.pk,
        }
        res = self.client.post(self.list_url, data)
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.name, data["name"])
        self.assertEqual(monitor.organization, self.organization)
        self.assertEqual(monitor.project, self.project)

    def test_create_invalid(self):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "",
            "expectedStatus": 200,
            "interval": "00:01:00",
            "project": self.project.pk,
        }
        res = self.client.post(self.list_url, data)
        self.assertEqual(res.status_code, 400)

    def test_monitor_retrieve(self):
        environment = baker.make(
            "environments.Environment", organization=self.organization
        )

        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            environment=environment,
        )

        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=True,
            start_check="2021-09-19T15:40:31Z",
        )

        url = reverse(
            "organization-monitors-detail",
            kwargs={"organization_slug": self.organization.slug, "pk": monitor.pk},
        )

        res = self.client.get(url)
        self.assertEqual(res.data["isUp"], True)
        self.assertEqual(res.data["lastChange"], "2021-09-19T15:39:31Z")
        self.assertEqual(res.data["environment"], environment.pk)

    def test_monitor_checks_list(self):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )

        url = reverse(
            "organization-monitor-checks-list",
            kwargs={
                "organization_slug": self.organization.slug,
                "monitor_pk": monitor.pk,
            },
        )

        res = self.client.get(url)
        self.assertContains(res, "2021-09-19T15:39:31Z")

    def test_monitor_update(self):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status="200",
        )

        url = reverse(
            "organization-monitors-detail",
            kwargs={"organization_slug": self.organization.slug, "pk": monitor.pk},
        )

        data = {
            "name": "New name",
            "url": "https://differentexample.com",
            "monitorType": "GET",
            "expectedStatus": "200",
            "interval": "60",
        }

        res = self.client.put(url, data, format="json")
        self.assertEqual(res.data["monitorType"], "GET")
        self.assertEqual(res.data["url"], "https://differentexample.com")

    @mock.patch("glitchtip.uptime.tasks.perform_checks.run")
    def test_list_isolation(self, _):
        """Users should only access monitors in their organization"""
        user2 = baker.make("users.user")
        org2 = baker.make("organizations_ext.Organization")
        org2.add_user(user2)
        monitor1 = baker.make("uptime.Monitor", organization=self.organization)
        monitor2 = baker.make("uptime.Monitor", organization=org2)

        res = self.client.get(self.list_url)
        self.assertContains(res, monitor1.name)
        self.assertNotContains(res, monitor2.name)

    def test_create_isolation(self):
        """Users should only make monitors in their organization"""
        org2 = baker.make("organizations_ext.Organization")

        url = reverse(
            "organization-monitors-list",
            kwargs={"organization_slug": org2.slug},
        )
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "interval": "00:01:00",
            "project": self.project.pk,
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)
