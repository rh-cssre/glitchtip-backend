from aioresponses import aioresponses
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker
from pydantic import ValidationError

from apps.projects.models import Project
from apps.teams.models import Team
from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from .importer import GlitchTipImporter
from .schema import ImportIn

test_project = {"id": "1", "slug": "project", "name": "project"}
test_key = {
    "id": "a" * 32,
    "public": "a" * 32,
    "projectId": 1,
    "label": "Default",
}


class ImporterTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.url = "https://example.com"
        self.org_name = "org"
        self.auth_token = "token"
        self.importer = GlitchTipImporter(
            self.url.lstrip("htps:/"), self.auth_token, self.org_name
        )

    def set_mocks(self, m):
        m.get(self.url + "/api/0/", payload={"user": {"username": "foo"}})
        m.get(self.url + self.importer.organization_url, payload={"id": 1})
        m.get(self.url + self.importer.organization_users_url, payload=[])
        m.get(self.url + self.importer.projects_url, payload=[test_project])
        m.get(self.url + "/api/0/projects/org/project/keys/", payload=[test_key])
        m.get(
            self.url + self.importer.teams_url,
            payload=[
                {
                    "id": "1",
                    "slug": "team",
                    "projects": [test_project],
                }
            ],
        )
        m.get(self.url + "/api/0/teams/org/team/members/", payload=[])

    @aioresponses()
    def test_import_command(self, m):
        self.set_mocks(m)

        call_command("import", self.url, self.auth_token, self.org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                slug=test_project["slug"],
                teams__slug="team",
                projectkey__public_key=test_key["public"],
            ).exists()
        )

    @aioresponses()
    def test_view(self, m):
        self.create_logged_in_user()
        self.organization.slug = self.org_name
        self.organization.save()
        self.set_mocks(m)
        url = reverse("api:importer")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": self.org_name,
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Team.objects.filter(slug="team").exists())

    @aioresponses()
    async def test_invalid_org(self, m):
        await sync_to_async(self.create_logged_in_user)()
        await self.async_client.aforce_login(self.user)
        url = reverse("api:importer")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": "foo",
        }
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)
        other_user = await baker.amake("users.User")
        other_org = await baker.amake("Organization", name="foo")
        await other_org.aadd_user(other_user)
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)
        await other_org.aadd_user(self.user)
        m.get(self.url + "api/0/", payload={"user": {"username": "foo"}})
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)


@override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=False)
class ImportInValidationTests(SimpleTestCase):
    """The import source URL is org-admin-supplied; reject internal targets."""

    def _validate(self, url):
        return ImportIn.model_validate(
            {"url": url, "authToken": "t", "organizationSlug": "o"}
        )

    def test_rejects_metadata_url(self):
        with self.assertRaises(ValidationError):
            self._validate("http://169.254.169.254/latest/")

    def test_rejects_loopback_url(self):
        with self.assertRaises(ValidationError):
            self._validate("http://127.0.0.1/api/0/")

    def test_accepts_public_url(self):
        self._validate("https://8.8.8.8/")  # public literal, no raise

    @override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=True)
    def test_allow_private_setting_permits_internal(self):
        self._validate("http://127.0.0.1/")
