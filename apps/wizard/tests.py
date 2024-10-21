from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from .constants import SETUP_WIZARD_CACHE_EMPTY, SETUP_WIZARD_CACHE_KEY


class WizardTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.url = reverse("api:setup_wizard")
        self.url_set_token = reverse("api:setup_wizard_set_token")

    def test_get_hash(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        wizard_hash = res.json().get("hash")
        self.assertEqual(len(wizard_hash), 64)
        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertEqual(cache.get(key), SETUP_WIZARD_CACHE_EMPTY)

    def test_set_token(self):
        res = self.client.get(self.url)
        wizard_hash = res.json().get("hash")

        self.client.force_login(self.user)
        res = self.client.post(
            self.url_set_token, {"hash": wizard_hash}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertTrue(cache.get(key)["apiKeys"])
        self.assertTrue(self.user.apitoken_set.exists())

        res = self.client.get(self.url + wizard_hash + "/")
        self.assertEqual(res.status_code, 200)
