from asgiref.sync import sync_to_async
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils import generators  # noqa: F401


class APITokenTests(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.url = reverse("api:list_api_tokens")

    def get_detail_url(self, id: int):
        return reverse("api:delete_api_token", args=[id])

    async def test_create(self):
        await self.async_client.aforce_login(self.user)
        scope_name = "member:read"
        data = {"scopes": [scope_name]}
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertContains(res, scope_name, status_code=201)

    async def test_list(self):
        await self.async_client.aforce_login(self.user)
        api_token = await baker.amake("api_tokens.APIToken", user=self.user)
        other_api_token = await baker.amake("api_tokens.APIToken")
        res = await self.async_client.get(self.url)
        self.assertContains(res, api_token.token)
        self.assertNotContains(res, other_api_token.token)

    async def test_destroy(self):
        await self.async_client.aforce_login(self.user)
        api_token = await baker.amake("api_tokens.APIToken", user=self.user)
        url = self.get_detail_url(api_token.id)
        self.assertTrue(await self.user.apitoken_set.aexists())
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(await self.user.apitoken_set.aexists())

        other_api_token = await baker.amake("api_tokens.APIToken")
        url = self.get_detail_url(other_api_token.id)
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)

    async def test_token_auth(self):
        """Token based auth should not be able to create it's own token"""
        organization = await baker.amake("organizations_ext.Organization")
        await sync_to_async(organization.add_user)(self.user)
        auth_token = await baker.amake("api_tokens.APIToken", user=self.user)

        auth_headers = {"HTTP_AUTHORIZATION": f"Bearer {auth_token.token}"}

        scope_name = "member:read"
        data = {"scopes": [scope_name]}
        res = await self.async_client.post(
            self.url, data, content_type="application/json", **auth_headers
        )
        self.assertEqual(res.status_code, 401)  # Was 403, might be better as 403

        res = await self.async_client.get(self.url, **auth_headers)
        self.assertEqual(res.status_code, 401)
