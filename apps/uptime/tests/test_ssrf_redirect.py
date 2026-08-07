"""Uptime monitor SSRF-via-redirect regression test.

A monitor whose (validated, public) URL redirects to an internal address must
report the check as failed and must never dial the internal target.
"""

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from apps.uptime.constants import MonitorCheckReason, MonitorType
from apps.uptime.utils import fetch
from glitchtip.test_utils.dns import resolves_to


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    async def release(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[str] = []

    async def request(self, method, url, **kwargs):
        self.requested.append(str(url))
        return self._responses.pop(0)


def _monitor(url):
    return {
        "id": 1,
        "organization_id": 1,
        "monitor_type": MonitorType.GET,
        "url": url,
        "timeout": 5,
        "expected_status": 200,
        "expected_body": "",
        "is_up": False,
        "latest_is_up": False,
        "reason": None,
        "response_time": None,
        "data": None,
    }


class UptimeRedirectSSRFTests(SimpleTestCase):
    def test_redirect_to_internal_is_blocked_and_not_dialed(self):
        # public.example resolves public, so the entry passes validation and we
        # exercise the redirect path exactly as a public redirector would.
        session = FakeSession(
            [FakeResponse(302, {"Location": "http://169.254.169.254/latest/"})]
        )
        with resolves_to():
            result = async_to_sync(fetch)(session, _monitor("http://public.example/"))
        self.assertFalse(result["is_up"])
        self.assertEqual(result["reason"], MonitorCheckReason.NETWORK)
        # Only the public entry was dialed; the metadata IP never was.
        self.assertEqual(session.requested, ["http://public.example/"])

    def test_normal_redirect_to_public_is_followed(self):
        session = FakeSession(
            [
                FakeResponse(302, {"Location": "http://final.example/"}),
                FakeResponse(200),
            ]
        )
        with resolves_to():
            result = async_to_sync(fetch)(session, _monitor("http://start.example/"))
        self.assertTrue(result["is_up"])
        self.assertEqual(
            session.requested, ["http://start.example/", "http://final.example/"]
        )


class PortMonitorSSRFTests(SimpleTestCase):
    def test_port_check_to_internal_literal_never_connects(self):
        monitor = _monitor("169.254.169.254:80")
        monitor["monitor_type"] = MonitorType.PORT

        async def boom(*_, **__):
            raise AssertionError("open_connection must not be called for a blocked IP")

        with mock.patch("apps.uptime.utils.asyncio.open_connection", boom):
            result = async_to_sync(fetch)(object(), monitor)
        self.assertFalse(result["is_up"])
        self.assertEqual(result["reason"], MonitorCheckReason.NETWORK)
