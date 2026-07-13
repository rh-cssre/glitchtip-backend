from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..models import Repository


class RepositoryAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)
        self.url = reverse(
            "api:list_repositories",
            kwargs={"organization_slug": self.organization.slug},
        )

    async def test_list_empty(self):
        res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    async def test_list(self):
        repo = await Repository.objects.acreate(
            organization=self.organization, name="my-repo"
        )
        res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], repo.name)
        self.assertIn("dateCreated", data[0])

    async def test_list_scoped_to_organization(self):
        await Repository.objects.acreate(organization=self.organization, name="my-repo")
        other_org = await baker.amake("organizations_ext.Organization")
        await Repository.objects.acreate(organization=other_org, name="other-repo")

        res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "my-repo")

    async def test_create(self):
        data = {
            "name": "my-repo",
            "url": "https://gitlab.com/org/my-repo",
        }
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["name"], "my-repo")
        self.assertEqual(body["url"], "https://gitlab.com/org/my-repo")
        self.assertTrue(
            await Repository.objects.filter(
                organization=self.organization, name="my-repo"
            ).aexists()
        )

    async def test_create_minimal(self):
        data = {"name": "minimal-repo"}
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["name"], "minimal-repo")
        self.assertEqual(body["status"], "active")

    async def test_create_duplicate_name(self):
        await Repository.objects.acreate(
            organization=self.organization, name="dup-repo"
        )
        data = {"name": "dup-repo"}
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 409)


class RepositoryAPIPermissionTestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.auth_token = baker.make("api_tokens.APIToken", user=self.user)
        self.url = reverse(
            "api:list_repositories",
            kwargs={"organization_slug": self.organization.slug},
        )

    def get_headers(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.auth_token.token}"}

    def test_get_requires_org_read(self):
        res = self.client.get(self.url, **self.get_headers())
        self.assertEqual(res.status_code, 403)

        self.auth_token.add_permission("org:read")
        res = self.client.get(self.url, **self.get_headers())
        self.assertEqual(res.status_code, 200)

    def test_post_requires_org_write(self):
        data = {"name": "test-repo"}

        self.auth_token.add_permission("org:read")
        res = self.client.post(
            self.url, data, content_type="application/json", **self.get_headers()
        )
        self.assertEqual(res.status_code, 403)

        self.auth_token.add_permission("org:write")
        res = self.client.post(
            self.url, data, content_type="application/json", **self.get_headers()
        )
        self.assertEqual(res.status_code, 201)
