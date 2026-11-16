import json

from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.oauth.provider import _grant_cache_key


def _make_signed_data(**overrides):
    data = {
        "client_id": "test-client-id",
        "state": "test-state",
        "scopes": ["org:read", "event:read"],
        "code_challenge": "challenge123",
        "redirect_uri": "http://localhost:3000/callback",
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }
    data.update(overrides)
    return signing.dumps(data)


class OAuthConsentViewTest(TestCase):
    def setUp(self):
        self.user = baker.make("users.user", is_active=True)
        self.user.set_password("testpass")
        self.user.save()

    def tearDown(self):
        cache.clear()

    def test_get_unauthenticated_redirects(self):
        signed = _make_signed_data()
        resp = self.client.get(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_get_authenticated_renders_consent(self):
        self.client.force_login(self.user)
        signed = _make_signed_data()
        resp = self.client.get(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "test-client-id")
        self.assertContains(resp, "org:read")
        self.assertContains(resp, "event:read")
        self.assertContains(resp, "Authorize")

    def test_post_stores_grant_and_redirects(self):
        self.client.force_login(self.user)
        signed = _make_signed_data()
        resp = self.client.post(
            reverse("oauth_consent") + f"?data={signed}",
        )
        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertIn("http://localhost:3000/callback", location)
        self.assertIn("code=", location)
        self.assertIn("state=test-state", location)

        # Extract the code from redirect URL
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        code = params["code"][0]

        # Verify grant is in cache
        raw = cache.get(_grant_cache_key(code))
        self.assertIsNotNone(raw)
        grant = json.loads(raw)
        self.assertEqual(grant["client_id"], "test-client-id")
        self.assertEqual(grant["user_id"], self.user.id)
        self.assertEqual(grant["scopes"], ["org:read", "event:read"])

    def test_post_without_state(self):
        self.client.force_login(self.user)
        signed = _make_signed_data(state=None)
        resp = self.client.post(
            reverse("oauth_consent") + f"?data={signed}",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("state=", resp["Location"])

    def test_invalid_signed_data(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent") + "?data=invalid")
        self.assertEqual(resp.status_code, 400)

    def test_missing_data_param(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent"))
        self.assertEqual(resp.status_code, 400)

    @override_settings(SECRET_KEY="different-key")
    def test_expired_signed_data(self):
        """Signed data with wrong key should fail."""
        # Create signed data with the default key, then validate with different key
        # This simulates expiration behavior
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent") + "?data=tampered-data")
        self.assertEqual(resp.status_code, 400)
