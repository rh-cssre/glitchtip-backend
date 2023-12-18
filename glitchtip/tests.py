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
    def test_settings_oidc(self, mock_get):
        # TODO not acceptable test
        social_app = baker.make(
            "socialaccount.socialapp",
            provider="openid_connect",
            settings={"server_url": "https://example.com"},
        )
        res = self.client.get(self.url)
        self.assertContains(res, social_app.name)
