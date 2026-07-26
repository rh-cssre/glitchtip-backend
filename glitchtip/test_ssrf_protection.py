"""Tests for connect-time SSRF protection (redirect + DNS-rebind bypass).

Regression coverage for the bypass where a URL that passes submission-time
validation is steered to an internal address via an HTTP redirect or DNS
rebinding. See :mod:`glitchtip.ssrf_protection`.
"""

import socket
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from glitchtip.ssrf_protection import (
    BlockedRedirectError,
    ValidatingResolver,
    request_with_validated_redirects,
    resolve_validated_ip,
)


class FakeInnerResolver:
    def __init__(self, hosts):
        self._hosts = hosts

    async def resolve(self, host, port=0, family=0):
        return self._hosts

    async def close(self):
        pass


def _addr(ip):
    return {
        "hostname": "example",
        "host": ip,
        "port": 80,
        "family": 2,
        "proto": 0,
        "flags": 0,
    }


class ValidatingResolverTests(SimpleTestCase):
    def _resolve(self, resolver):
        return async_to_sync(resolver.resolve)("host.example", 80)

    def test_blocks_when_only_internal_addresses(self):
        resolver = ValidatingResolver(allow_private=False)
        resolver._resolver = FakeInnerResolver([_addr("10.0.0.1")])
        with self.assertRaises(OSError):
            self._resolve(resolver)

    def test_blocks_metadata_address(self):
        resolver = ValidatingResolver(allow_private=False)
        resolver._resolver = FakeInnerResolver([_addr("169.254.169.254")])
        with self.assertRaises(OSError):
            self._resolve(resolver)

    def test_allows_public_address(self):
        resolver = ValidatingResolver(allow_private=False)
        resolver._resolver = FakeInnerResolver([_addr("93.184.216.34")])
        result = self._resolve(resolver)
        self.assertEqual(result[0]["host"], "93.184.216.34")

    def test_filters_internal_from_mixed_result(self):
        # A public+internal split-horizon answer must not leak the internal one.
        resolver = ValidatingResolver(allow_private=False)
        resolver._resolver = FakeInnerResolver(
            [_addr("10.0.0.1"), _addr("93.184.216.34")]
        )
        result = self._resolve(resolver)
        self.assertEqual([h["host"] for h in result], ["93.184.216.34"])

    def test_allow_private_returns_everything(self):
        resolver = ValidatingResolver(allow_private=True)
        resolver._resolver = FakeInnerResolver([_addr("10.0.0.1")])
        result = self._resolve(resolver)
        self.assertEqual(result[0]["host"], "10.0.0.1")


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}
        self.released = False

    async def release(self):
        self.released = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[tuple[str, str]] = []

    async def request(self, method, url, **kwargs):
        self.requested.append((method, str(url)))
        assert kwargs.get("allow_redirects") is False, "redirects must be disabled"
        return self._responses.pop(0)


# ".example" is a reserved, non-resolving TLD, so check_url_safe's getaddrinfo
# fails and (by design) treats these hostnames as allowed — letting these tests
# exercise the redirect logic without real DNS.
class RequestWithValidatedRedirectsTests(SimpleTestCase):
    def _run(self, session, method, url, **kwargs):
        return async_to_sync(request_with_validated_redirects)(
            session, method, url, **kwargs
        )

    def test_blocks_redirect_to_metadata_ip_literal(self):
        # The reported PoC: a public URL 302-redirects to an internal IP literal.
        session = FakeSession(
            [FakeResponse(302, {"Location": "http://169.254.169.254/latest/"})]
        )
        with self.assertRaises(BlockedRedirectError):
            self._run(session, "GET", "https://public.example/")
        # The internal target was never dialed.
        self.assertEqual(session.requested, [("GET", "https://public.example/")])

    def test_blocks_redirect_to_loopback(self):
        session = FakeSession(
            [FakeResponse(302, {"Location": "http://127.0.0.1:9000/secret"})]
        )
        with self.assertRaises(BlockedRedirectError):
            self._run(session, "GET", "https://public.example/")
        self.assertEqual(len(session.requested), 1)

    def test_follows_redirect_to_public_target(self):
        session = FakeSession(
            [
                FakeResponse(302, {"Location": "https://final.example/ok"}),
                FakeResponse(200),
            ]
        )
        resp = self._run(session, "GET", "https://start.example/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            session.requested,
            [("GET", "https://start.example/"), ("GET", "https://final.example/ok")],
        )

    def test_302_downgrades_post_to_get(self):
        session = FakeSession(
            [
                FakeResponse(302, {"Location": "https://final.example/"}),
                FakeResponse(200),
            ]
        )
        self._run(session, "POST", "https://start.example/")
        self.assertEqual(session.requested[1][0], "GET")

    def test_307_preserves_method(self):
        session = FakeSession(
            [
                FakeResponse(307, {"Location": "https://final.example/"}),
                FakeResponse(200),
            ]
        )
        self._run(session, "POST", "https://start.example/")
        self.assertEqual(session.requested[1][0], "POST")

    def test_caps_redirect_chain(self):
        responses = [
            FakeResponse(302, {"Location": f"https://h{i}.example/"}) for i in range(20)
        ]
        session = FakeSession(responses)
        with self.assertRaises(BlockedRedirectError):
            self._run(session, "GET", "https://start.example/", max_redirects=3)
        self.assertEqual(len(session.requested), 4)  # initial + 3 follows


class ResolveValidatedIPTests(SimpleTestCase):
    def _run(self, host, allow_private=False):
        return async_to_sync(resolve_validated_ip)(host, allow_private)

    def test_public_literal_returned(self):
        self.assertEqual(self._run("8.8.8.8"), "8.8.8.8")

    def test_internal_literal_blocked(self):
        with self.assertRaises(BlockedRedirectError):
            self._run("169.254.169.254")
        with self.assertRaises(BlockedRedirectError):
            self._run("127.0.0.1")

    def test_allow_private_returns_internal_literal(self):
        self.assertEqual(self._run("10.0.0.1", allow_private=True), "10.0.0.1")

    def test_hostname_resolving_to_internal_is_blocked(self):
        async def fake_getaddrinfo(*_, **__):
            return [(0, 0, 0, "", ("10.0.0.5", 0))]

        with (
            mock.patch(
                "asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True
            ),
            self.assertRaises(BlockedRedirectError),
        ):
            self._run("rebind.example")

    def test_hostname_pins_first_public_ip(self):
        async def fake_getaddrinfo(*_, **__):
            return [
                (0, 0, 0, "", ("10.0.0.5", 0)),  # internal, must be skipped
                (0, 0, 0, "", ("93.184.216.34", 0)),
            ]

        with mock.patch(
            "asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True
        ):
            self.assertEqual(self._run("mixed.example"), "93.184.216.34")

    def test_unresolvable_host_blocked(self):
        async def fake_getaddrinfo(*_, **__):
            raise socket.gaierror("nope")

        with (
            mock.patch(
                "asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True
            ),
            self.assertRaises(BlockedRedirectError),
        ):
            self._run("no-such-host.invalid")
