import json

from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.uptime.models import Monitor, StatusPage
from glitchtip.test_utils.test_case import APIPermissionTestCase, GlitchTestCase


class UptimeRoleTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.monitor = baker.make(
            "uptime.Monitor", organization=cls.organization, url="http://example.com"
        )

    def setUp(self):
        self.async_client.force_login(self.user)

    async def test_member_cannot_write(self):
        """Uptime writes take project:write, so admin and up."""
        self.org_user.role = OrganizationUserRole.MEMBER
        await self.org_user.asave()
        org = self.organization.slug
        detail = reverse("api:update_monitor", args=[org, self.monitor.id])
        monitor = {
            "monitorType": "Ping",
            "name": "m",
            "url": "https://example.com",
            "expectedStatus": 200,
            "expectedBody": "",
            "interval": 60,
            "timeout": 25,
        }
        for method, url, data in (
            ("POST", reverse("api:create_monitor", args=[org]), monitor),
            ("PUT", detail, monitor),
            ("DELETE", detail, None),
            ("POST", reverse("api:create_status_page", args=[org]), {"name": "s"}),
        ):
            with self.subTest(method=method, url=url):
                res = await self.async_client.generic(
                    method,
                    url,
                    "" if data is None else json.dumps(data),
                    content_type="application/json",
                )
                self.assertEqual(res.status_code, 403)
        self.assertEqual(await Monitor.objects.acount(), 1)
        self.assertFalse(await StatusPage.objects.aexists())

    async def test_status_pages_are_listed_for_the_path_org_only(self):
        other_org = await baker.amake("organizations_ext.Organization")
        await other_org.aadd_user(self.user)
        await baker.amake(
            "uptime.StatusPage", organization=self.organization, name="ours"
        )
        await baker.amake("uptime.StatusPage", organization=other_org, name="theirs")
        res = await self.async_client.get(
            reverse("api:list_status_pages", args=[self.organization.slug])
        )
        self.assertEqual([page["name"] for page in res.json()], ["ours"])


class UptimeTokenScopeTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.list_url = reverse("api:list_monitors", args=[self.organization.slug])

    def test_list_requires_org_read(self):
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("org:read")
        self.assertGetReqStatusCode(self.list_url, 200)
