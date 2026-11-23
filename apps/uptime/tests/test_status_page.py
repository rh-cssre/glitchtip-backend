from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase

from ..constants import MonitorType


class StatusPageTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_status_page(self):
        status_page = await baker.amake(
            "uptime.StatusPage", organization=self.organization, is_public=False
        )
        url = status_page.get_absolute_url()
        res = await self.async_client.get(url)
        self.assertContains(res, status_page.name)

        await self.async_client.alogout()
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 404)

        status_page.is_public = True
        await status_page.asave()
        res = await self.async_client.get(url)
        self.assertContains(res, status_page.name)

    async def test_status_page_with_monitors(self):
        """Monitors should be cached and displayed on the status page.

        Regression test: caching Monitor model instances directly fails
        with msgpack-based cache backends (vcache). The view must cache
        serializable dicts instead.
        """
        status_page = await baker.amake(
            "uptime.StatusPage",
            organization=self.organization,
            is_public=True,
        )
        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            name="Test Monitor",
        )
        await status_page.monitors.aadd(monitor)

        url = status_page.get_absolute_url()
        res = await self.async_client.get(url)
        self.assertContains(res, "Test Monitor")

        # Second request should serve from cache
        res = await self.async_client.get(url)
        self.assertContains(res, "Test Monitor")

    async def test_status_page_api(self):
        status_page = await baker.amake(
            "uptime.StatusPage", organization=self.organization
        )
        other_status_page = await baker.amake("uptime.StatusPage")
        url = reverse("api:list_status_pages", args=(self.organization.slug,))
        res = await self.async_client.get(url)
        self.assertContains(res, status_page.name)
        self.assertNotContains(res, other_status_page.name)

    async def test_status_page_api_with_monitors(self):
        """The JSON API must serialize a status page's monitors in the async
        request without triggering a synchronous ORM query.

        Regression: the endpoint prefetched ``monitors`` with the default
        manager, so serializing each monitor lazily loaded its checks
        (and, for heartbeat monitors, its organization) and raised
        SynchronousOnlyOperation whenever a status page had monitors.
        """
        project = await baker.amake(
            "projects.Project", organization=self.organization, name="Prod"
        )
        status_page = await baker.amake(
            "uptime.StatusPage", organization=self.organization
        )
        monitor = await baker.amake(
            "uptime.Monitor",
            organization=self.organization,
            project=project,
            name="Prod Monitor",
            monitor_type=MonitorType.HEARTBEAT,
        )
        await status_page.monitors.aadd(monitor)

        url = reverse("api:list_status_pages", args=(self.organization.slug,))
        res = await self.async_client.get(url)

        self.assertEqual(res.status_code, 200)
        page = res.json()[0]
        serialized_monitor = page["monitors"][0]
        self.assertEqual(serialized_monitor["name"], "Prod Monitor")
        self.assertEqual(serialized_monitor["projectName"], "Prod")
        # Heartbeat monitors expose an endpoint URL built from the organization.
        self.assertIn(self.organization.slug, serialized_monitor["heartbeatEndpoint"])

    async def test_status_page_api_shared_monitor_checks_not_duplicated(self):
        """A monitor shared across status pages must not have doubled checks.

        The monitors M2M yields a distinct instance per (status_page, monitor)
        pair, so a shared monitor appears once per page. The check fetch must
        dedupe by monitor id, otherwise the LATERAL join runs per copy and the
        serialized ``checks`` list is returned N times over.
        """
        monitor = await baker.amake(
            "uptime.Monitor", organization=self.organization, name="Shared"
        )
        await baker.amake(
            "uptime.MonitorCheck",
            monitor=monitor,
            organization=self.organization,
            is_up=True,
        )
        for name in ("Page A", "Page B"):
            page = await baker.amake(
                "uptime.StatusPage", organization=self.organization, name=name
            )
            await page.monitors.aadd(monitor)

        url = reverse("api:list_status_pages", args=(self.organization.slug,))
        res = await self.async_client.get(url)

        self.assertEqual(res.status_code, 200)
        pages = res.json()
        self.assertEqual(len(pages), 2)
        for status_page in pages:
            # Exactly the one check created, not doubled by the shared monitor.
            self.assertEqual(len(status_page["monitors"][0]["checks"]), 1)

    async def test_status_page_api_create(self):
        url = reverse("api:create_status_page", args=(self.organization.slug,))
        data = {"name": "foo"}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, data["name"], status_code=201)
