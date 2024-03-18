from django.core.cache import cache
from django.urls import reverse

from glitchtip.test_utils.test_case import GlitchTipTestCase

from .views import SETUP_WIZARD_CACHE_EMPTY, SETUP_WIZARD_CACHE_KEY


class WizardTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("setup-wizard")
        self.url_set_token = reverse("setup-wizard-set-token")

    def test_get_hash(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        wizard_hash = res.data.get("hash")
        self.assertEqual(len(wizard_hash), 64)
        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertEqual(cache.get(key), SETUP_WIZARD_CACHE_EMPTY)

    def test_set_token(self):
        res = self.client.get(self.url)
        wizard_hash = res.data.get("hash")

        self.client.force_login(self.user)
        res = self.client.post(self.url_set_token, {"hash": wizard_hash})
        self.assertEqual(res.status_code, 200)

        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertTrue(cache.get(key)["apiKeys"])
        self.assertTrue(self.user.apitoken_set.exists())

        res = self.client.get(self.url + wizard_hash + "/")
        self.assertEqual(res.status_code, 200)
