from unittest import mock

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

    @mock.patch(
        "allauth.socialaccount.providers.openid_connect.views.OpenIDConnectAdapter.openid_config",
        new_callable=lambda: {"authorization_endpoint": ""},
    )
    def test_settings_oidc(self, _):
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
        res = self.client.get(self.url)
        self.assertContains(res, social_app.name)
