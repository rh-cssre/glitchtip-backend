"""Regression coverage for the allauth adapter overrides in glitchtip.adapters.

CustomSocialAccountAdapter.open_http_session builds the aiohttp session used
for every outbound social-auth request (OAuth token exchange, userinfo). It
merges the project-wide AIOHTTP_CONFIG (User-Agent, proxy trust, max field
size) with allauth's own per-call REQUESTS_TIMEOUT. AIOHTTP_CONFIG already
carries a `timeout` key, so the two timeout sources must be merged, not passed
as separate kwargs -- otherwise ClientSession() raises TypeError ("got multiple
values for keyword argument 'timeout'") and every Google login callback 500s.
"""

import asyncio

import aiohttp
from allauth.socialaccount import app_settings as socialaccount_app_settings
from django.test import SimpleTestCase

from glitchtip.adapters import CustomSocialAccountAdapter


class OpenHttpSessionTestCase(SimpleTestCase):
    def _open_and_close(self):
        adapter = CustomSocialAccountAdapter()

        async def run():
            # ClientSession must be created inside a running loop.
            session = adapter.open_http_session()
            try:
                return session.timeout.total, dict(session.headers)
            finally:
                await session.close()

        return asyncio.run(run())

    def test_open_http_session_does_not_collide_on_timeout(self):
        # The bug was a TypeError here; simply reaching the assertions proves
        # the duplicate-kwarg collision is gone.
        total, _ = self._open_and_close()
        self.assertEqual(total, socialaccount_app_settings.REQUESTS_TIMEOUT)

    def test_open_http_session_keeps_project_config(self):
        # AIOHTTP_CONFIG's non-timeout entries (e.g. the User-Agent) still apply.
        _, headers = self._open_and_close()
        self.assertTrue(headers.get("User-Agent", "").startswith("GlitchTip/"))

    def test_open_http_session_is_a_client_session(self):
        adapter = CustomSocialAccountAdapter()

        async def run():
            session = adapter.open_http_session()
            try:
                self.assertIsInstance(session, aiohttp.ClientSession)
            finally:
                await session.close()

        asyncio.run(run())
