from unittest import mock

from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from freezegun import freeze_time
from model_bakery import baker

from apps.uptime.models import Monitor, MonitorCheck
from glitchtip.test_utils.test_case import GlitchTestCase


class UptimeAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse(
            "api:list_monitors",
            args=[cls.organization.slug],
        )

    def setUp(self):
        self.client.force_login(self.user)

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_list(self, mocked):
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
            is_change=True,
            start_check="2021-09-19T15:40:31Z",
        )
        res = self.client.get(self.list_url)
        self.assertContains(res, monitor.name)
        data = res.json()
        self.assertEqual(data[0]["isUp"], True)
        self.assertEqual(data[0]["lastChange"], "2021-09-19T15:40:31Z")

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_list_aggregation(self, _):
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
        self.assertEqual(len(res.json()[0]["checks"]), 60)

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_create_http_monitor(self, mocked):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 60,
            "project": str(self.project.pk),
            "timeout": 25,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.name, data["name"])
        self.assertEqual(monitor.timeout, data["timeout"])
        self.assertEqual(monitor.organization, self.organization)
        self.assertEqual(monitor.project, self.project)
        mocked.assert_called_once()

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_create_port_monitor(self, mocked):
        """Port monitor URLs should be converted to domain:port format, with protocol removed"""
        data = {
            "monitorType": "TCP Port",
            "name": "Test",
            "url": "http://example.com:80",
            "expectedStatus": None,
            "expectedBody": "",
            "timeout": None,
            "interval": 60,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.url, "example.com:80")
        mocked.assert_called_once()

    def test_create_port_monitor_validation(self):
        """Port monitor URLs should be converted to domain:port format, with protocol removed"""
        data = {
            "monitorType": "TCP Port",
            "name": "Test",
            "url": "example:80:",
            "expectedStatus": None,
            "expectedBody": "",
            "timeout": None,
            "interval": 60,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    def test_create_invalid(self):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "foo:80:",
            "interval": 60,
            "expectedStatus": 200,
            "expectedBody": "",
            "timeout": None,
            "project": self.project.pk,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 60,
            "project": self.project.pk,
            "timeout": 999,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_create_expected_status(self, mocked):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "http://example.com",
            "expectedStatus": None,
            "expectedBody": "",
            "timeout": None,
            "interval": 60,
            "project": str(self.project.pk),
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        mocked.assert_called_once()
        self.assertEqual(res.status_code, 201)
        self.assertTrue(Monitor.objects.filter(expected_status=None).exists())

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_monitor_retrieve(self, _):
        """Test monitor details endpoint. Unlike the list view,
        checks here should include response time for the frontend graph"""
        environment = baker.make(
            "environments.Environment", organization=self.organization
        )

        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            monitor_type="Heartbeat",
            environment=environment,
        )

        now = timezone.now()
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=False,
            is_change=True,
            start_check="2021-09-19T15:39:31Z",
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=True,
            is_change=True,
            start_check=now,
        )

        url = reverse("api:get_monitor", args=[self.organization.slug, monitor.pk])
        res = self.client.get(url)
        data = res.json()
        self.assertEqual(data["isUp"], True)
        self.assertEqual(parse_datetime(data["lastChange"]), now)
        self.assertEqual(data["environmentID"], environment.pk)
        self.assertIn("responseTime", data["checks"][0])

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_monitor_checks_list(self, _):
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
            "api:list_monitor_checks", args=[self.organization.slug, monitor.pk]
        )

        res = self.client.get(url)
        self.assertContains(res, "2021-09-19T15:39:31Z")

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_monitor_update(self, _):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status=None,
        )

        url = reverse("api:update_monitor", args=[self.organization.slug, monitor.pk])
        data = {
            "name": monitor.name,
            "url": "https://differentexample.com",
            "monitorType": "Ping",
            "interval": 60,
            "expectedBody": "",
            "expected_status": None,
            "timeout": 20,
            "project": str(self.project.id),
        }

        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["projectID"], str(self.project.id))
        self.assertEqual(res.json()["url"], "https://differentexample.com")

        data = {
            "name": monitor.name,
            "url": "https://differentexample.com",
            "monitorType": "GET",
            "interval": 60,
            "expectedBody": "test",
            "expected_status": None,
            "timeout": 20,
            "project": str(self.project.id),
        }

        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

        data = {
            "name": monitor.name,
            "url": "https://differentexample.com",
            "monitorType": "GET",
            "interval": 60,
            "expectedBody": "",
            "expected_status": 422,
            "timeout": None,
            "project": str(self.project.id),
        }

        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["monitorType"], "GET")
        self.assertEqual(res.json()["expectedBody"], "")
        self.assertEqual(res.json()["timeout"], None)

    def test_monitor_delete(self):
        monitor = baker.make(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status=None,
        )
        baker.make(
            "uptime.MonitorCheck",
            monitor=monitor,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )

        url = reverse("api:delete_monitor", args=[self.organization.slug, monitor.pk])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(Monitor.objects.count(), 0)
        self.assertEqual(MonitorCheck.objects.count(), 0)

        another_org = baker.make("organizations_ext.Organization")
        another_monitor = baker.make(
            "uptime.Monitor",
            organization=another_org,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status=None,
        )

        url = reverse("api:delete_monitor", args=[another_org.slug, another_monitor.pk])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)

    @mock.patch("apps.uptime.tasks.perform_checks.run")
    def test_list_isolation(self, _):
        """Users should only access monitors in their organization"""
        user2 = baker.make("users.user")
        org2 = baker.make("organizations_ext.Organization")
        org2.add_user(user2)
        monitor1 = baker.make(
            "uptime.Monitor", url="http://example.com", organization=self.organization
        )
        monitor2 = baker.make(
            "uptime.Monitor", url="http://example.com", organization=org2
        )

        res = self.client.get(self.list_url)
        self.assertContains(res, monitor1.name)
        self.assertNotContains(res, monitor2.name)

    def test_create_isolation(self):
        """Users should only make monitors in their organization"""
        org2 = baker.make("organizations_ext.Organization")

        url = reverse("api:list_monitors", args=[org2.slug])
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "interval": 60,
            "project": self.project.pk,
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)
