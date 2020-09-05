from django.urls import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import


class APITokenTests(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")

    def test_create(self):
        self.client.force_login(self.user)
        url = reverse("api-tokens-list")
        scope_name = "member:read"
        data = {"scopes": [scope_name]}
        res = self.client.post(url, data, format="json")
        self.assertContains(res, scope_name, status_code=201)

    def test_list(self):
        self.client.force_login(self.user)
        api_token = baker.make("api_tokens.APIToken", user=self.user)
        other_api_token = baker.make("api_tokens.APIToken")
        url = reverse("api-tokens-list")
        res = self.client.get(url)
        self.assertContains(res, api_token.token)
        self.assertNotContains(res, other_api_token.token)

    def test_retrieve(self):
        self.client.force_login(self.user)
        api_token = baker.make("api_tokens.APIToken", user=self.user)
        url = reverse("api-tokens-detail", args=[api_token.id])
        res = self.client.get(url)
        self.assertContains(res, api_token.token)

        other_api_token = baker.make("api_tokens.APIToken")
        res = self.client.get(reverse("api-tokens-detail", args=[other_api_token.id]))
        self.assertEqual(res.status_code, 404)

    def test_destroy(self):
        self.client.force_login(self.user)
        api_token = baker.make("api_tokens.APIToken", user=self.user)
        url = reverse("api-tokens-detail", args=[api_token.id])
        self.assertTrue(self.user.apitoken_set.exists())
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(self.user.apitoken_set.exists())

        other_api_token = baker.make("api_tokens.APIToken")
        url = reverse("api-tokens-detail", args=[other_api_token.id])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 404)

    def test_token_auth(self):
        """ Token based auth should not be able to create it's own token """
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user)
        auth_token = baker.make("api_tokens.APIToken", user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + auth_token.token)

        url = reverse("api-tokens-list")
        scope_name = "member:read"
        data = {"scopes": [scope_name]}
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 403)

        res = self.client.get(url)
        self.assertEqual(res.status_code, 403)