from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from .constants import SETUP_WIZARD_CACHE_EMPTY, SETUP_WIZARD_CACHE_KEY


class WizardTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)
        self.url = reverse("api:setup_wizard")
        self.url_set_token = reverse("api:setup_wizard_set_token")

    async def test_get_hash(self):
        res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        wizard_hash = res.json().get("hash")
        self.assertEqual(len(wizard_hash), 64)
        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertEqual(await cache.aget(key), SETUP_WIZARD_CACHE_EMPTY)

    async def test_set_token(self):
        res = await self.async_client.get(self.url)
        wizard_hash = res.json().get("hash")

        await self.async_client.aforce_login(self.user)
        res = await self.async_client.post(
            self.url_set_token, {"hash": wizard_hash}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        self.assertTrue((await cache.aget(key))["apiKeys"])
        self.assertTrue(await self.user.apitoken_set.aexists())

        res = await self.async_client.get(self.url + wizard_hash + "/")
        self.assertEqual(res.status_code, 200)
