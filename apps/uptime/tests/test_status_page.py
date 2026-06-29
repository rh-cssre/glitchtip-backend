from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase


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

    async def test_status_page_api_create(self):
        url = reverse("api:create_status_page", args=(self.organization.slug,))
        data = {"name": "foo"}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, data["name"], status_code=201)
