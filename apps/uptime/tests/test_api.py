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
        self.async_client.force_login(self.user)

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_list(self, mocked):
        monitor = await baker.amake(
            "uptime.Monitor", organization=self.organization, url="http://example.com"
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=True,
            is_change=True,
            start_check="2021-09-19T15:40:31Z",
        )
        monitor.cached_is_up = True
        monitor.cached_last_change = parse_datetime("2021-09-19T15:40:31Z")
        await monitor.asave(update_fields=["cached_is_up", "cached_last_change"])
        res = await self.async_client.get(self.list_url)
        self.assertContains(res, monitor.name)
        data = res.json()
        self.assertEqual(data[0]["isUp"], True)
        self.assertEqual(data[0]["lastChange"], "2021-09-19T15:40:31Z")

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_list_aggregation(self, _):
        """Test up and down event aggregations"""
        monitor = await baker.amake(
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
                await baker.amake(
                    "uptime.MonitorCheck",
                    monitor=monitor,
                    organization=monitor.organization,
                    is_up=is_up,
                    start_check=current_time,
                )
        with freeze_time(current_time):
            res = await self.async_client.get(self.list_url)
        self.assertEqual(len(res.json()[0]["checks"]), 60)

    # Kept synchronous: relies on captureOnCommitCallbacks + a sync mock
    # assertion. on_commit callbacks fired by the async request run on the
    # async DB connection, which the sync capture context cannot observe.
    @mock.patch("apps.uptime.tasks.perform_checks")
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
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.name, data["name"])
        self.assertEqual(monitor.timeout, data["timeout"])
        self.assertEqual(monitor.organization, self.organization)
        self.assertEqual(monitor.project, self.project)
        # Defaults to the current behavior (alert on first failure) when omitted
        self.assertEqual(monitor.confirmation_threshold, 1)
        mocked.enqueue.assert_called_once()

    # Kept synchronous: see note on test_create_http_monitor.
    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_create_monitor_confirmation_threshold(self, mocked):
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 60,
            "project": str(self.project.pk),
            "timeout": 25,
            "confirmationThreshold": 3,
        }
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["confirmationThreshold"], 3)
        monitor = Monitor.objects.get()
        self.assertEqual(monitor.confirmation_threshold, 3)

    @mock.patch("apps.uptime.tasks.perform_checks")
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
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.url, "example.com:80")
        mocked.enqueue.assert_called_once()

    async def test_create_port_monitor_validation(self):
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
        res = await self.async_client.post(
            self.list_url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 422)

    async def test_create_invalid(self):
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
        res = await self.async_client.post(
            self.list_url, data, content_type="application/json"
        )
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
        res = await self.async_client.post(
            self.list_url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 422)

    # Kept synchronous: see note on test_create_http_monitor.
    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_create_max_interval(self, mocked):
        """A one-day interval (86400) is allowed by the validator and must not
        overflow the column (previously a smallint, max 32767)."""
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 86400,
            "project": str(self.project.pk),
            "timeout": 25,
        }
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.interval, 86400)

    def test_create_over_max_interval(self):
        """An interval above the 86400 ceiling must be rejected at the API
        layer (clean 4xx) rather than reaching the DB and raising a 500."""
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 86401,
            "project": str(self.project.pk),
            "timeout": 25,
        }
        res = self.client.post(self.list_url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(Monitor.objects.count(), 0)

    @mock.patch("apps.uptime.tasks.perform_checks")
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
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.list_url, data, content_type="application/json")
        mocked.enqueue.assert_called_once()
        self.assertEqual(res.status_code, 201)
        self.assertTrue(Monitor.objects.filter(expected_status=None).exists())

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_monitor_retrieve(self, _):
        """Test monitor details endpoint. Unlike the list view,
        checks here should include response time for the frontend graph"""
        environment = await baker.amake(
            "environments.Environment", organization=self.organization
        )

        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            monitor_type="Heartbeat",
            environment=environment,
        )

        now = timezone.now()
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=False,
            is_change=True,
            start_check="2021-09-19T15:39:31Z",
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=True,
            is_change=True,
            start_check=now,
        )
        monitor.cached_is_up = True
        monitor.cached_last_change = now
        await monitor.asave(update_fields=["cached_is_up", "cached_last_change"])

        url = reverse("api:get_monitor", args=[self.organization.slug, monitor.pk])
        res = await self.async_client.get(url)
        data = res.json()
        self.assertEqual(data["isUp"], True)
        self.assertEqual(parse_datetime(data["lastChange"]), now)
        self.assertEqual(data["environmentID"], environment.pk)
        self.assertIn("responseTime", data["checks"][0])

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_monitor_checks_list(self, _):
        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )

        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, monitor.pk]
        )

        res = await self.async_client.get(url)
        self.assertContains(res, "2021-09-19T15:39:31Z")

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_monitor_checks_is_change_baseline(self, _):
        """When all is_change=True records have been pruned (e.g. partition
        retention on a 100% uptime monitor), the is_change=true filter should
        still return at least one record — the most recent check should be
        marked as a baseline change."""
        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
        )
        # Simulate post-pruning state: only is_change=False checks remain
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=True,
            is_change=False,
            start_check="2021-09-19T15:39:31Z",
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=True,
            is_change=False,
            start_check="2021-09-19T15:40:31Z",
        )

        url = reverse(
            "api:list_monitor_checks", args=[self.organization.slug, monitor.pk]
        )

        # Without filter, all checks are returned
        res = await self.async_client.get(url)
        self.assertEqual(len(res.json()), 2)

        # With is_change=true, should return nothing (no baseline yet)
        res = await self.async_client.get(url + "?is_change=true")
        self.assertEqual(len(res.json()), 0)

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_monitor_update(self, _):
        monitor = await baker.amake(
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

        res = await self.async_client.put(url, data, content_type="application/json")
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

        res = await self.async_client.put(url, data, content_type="application/json")
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

        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["monitorType"], "GET")
        self.assertEqual(res.json()["expectedBody"], "")
        self.assertEqual(res.json()["timeout"], None)

    async def test_monitor_delete(self):
        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status=None,
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=monitor.organization,
            is_up=False,
            start_check="2021-09-19T15:39:31Z",
        )

        url = reverse("api:delete_monitor", args=[self.organization.slug, monitor.pk])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await Monitor.objects.acount(), 0)
        self.assertEqual(await MonitorCheck.objects.acount(), 0)

        another_org = await baker.amake("organizations_ext.Organization")
        another_monitor = await baker.amake(
            "uptime.Monitor",
            organization=another_org,
            url="http://example.com",
            interval="60",
            monitor_type="Ping",
            expected_status=None,
        )

        url = reverse("api:delete_monitor", args=[another_org.slug, another_monitor.pk])
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)

    @mock.patch("apps.uptime.tasks.perform_checks")
    async def test_list_isolation(self, _):
        """Users should only access monitors in their organization"""
        user2 = await baker.amake("users.user")
        org2 = await baker.amake("organizations_ext.Organization")
        await org2.aadd_user(user2)
        monitor1 = await baker.amake(
            "uptime.Monitor", url="http://example.com", organization=self.organization
        )
        monitor2 = await baker.amake(
            "uptime.Monitor", url="http://example.com", organization=org2
        )

        res = await self.async_client.get(self.list_url)
        self.assertContains(res, monitor1.name)
        self.assertNotContains(res, monitor2.name)

    async def test_create_isolation(self):
        """Users should only make monitors in their organization"""
        org2 = await baker.amake("organizations_ext.Organization")

        url = reverse("api:list_monitors", args=[org2.slug])
        data = {
            "monitorType": "Ping",
            "name": "Test",
            "url": "https://www.google.com",
            "expectedStatus": 200,
            "interval": 60,
            "project": self.project.pk,
        }
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)
