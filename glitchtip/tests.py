import requests_mock
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker


class SettingsTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:get_settings")

    def test_settings(self):
        with self.assertNumQueries(1):
            res = self.client.get(self.url)  # Check that no auth is necessary
        self.assertEqual(res.status_code, 200)

    def test_settings_oidc(self):
        social_app = baker.make(
            "socialaccount.socialapp",
            provider="openid_connect",
            provider_id="my-openid",
            settings={"server_url": "https://example.com"},
        )
        for provider in [
            "gitlab",
            "microsoft",
            "github",
            "google",
            "nextcloud",
            "digitalocean",
        ]:
            baker.make(
                "socialaccount.socialapp",
                provider=provider,
            )
        with requests_mock.Mocker() as m:
            m.get(
                "https://example.com/.well-known/openid-configuration",
                json={"authorization_endpoint": ""},
            )
            res = self.client.get(self.url)
        self.assertContains(res, social_app.name)


class APIRootTestCase(TestCase):
    def setUp(self):
        self.url = reverse("api:api_root")

    def test_anon(self):
        self.assertContains(self.client.get(self.url), "version")

    def test_user(self):
        user = baker.make("users.user")
        self.client.force_login(user)
        res = self.client.get(self.url)
        self.assertContains(res, user.email)

    def test_token(self):
        user = baker.make("users.user")
        auth_token = baker.make("api_tokens.APIToken", user=user)

        headers = {"Authorization": f"Bearer {auth_token.token}"}
        res = self.client.get(self.url, headers=headers)
        self.assertContains(res, auth_token.token)
        self.assertContains(res, user.email)


class InternalHealthTestCase(TestCase):
    def setUp(self):
        self.url = "/api/0/internal/health"

    def test_get_health(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)

        data = res.json()
        self.assertIn("healthy", data)
        self.assertIn("problems", data)
