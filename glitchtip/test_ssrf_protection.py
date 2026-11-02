"""Tests for connect-time SSRF protection (redirect + DNS-rebind bypass).

Regression coverage for the bypass where a URL that passes submission-time
validation is steered to an internal address via an HTTP redirect or DNS
rebinding. See :mod:`glitchtip.ssrf_protection`.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

import aiohttp
from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import SimpleTestCase

from glitchtip.ssrf_protection import (
    BlockedRedirectError,
    ValidatingConnector,
    ValidatingResolver,
    request_with_validated_redirects,
    resolve_validated_ip,
    safe_session,
    session_kwargs,
)
from glitchtip.test_utils.dns import resolves_to


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


class ValidatingConnectorTests(SimpleTestCase):
    def _resolve(self, host, port=443, **connector_kwargs):
        async def run():
            connector = ValidatingConnector(**connector_kwargs)
            try:
                return await connector._resolve_host(host, port)
            finally:
                await connector.close()

        return async_to_sync(run)()

    def test_rejects_internal_ip_literals(self):
        # TCPConnector never consults the resolver for IP-literal hosts, so the
        # connector itself must reject them (e.g. an OIDC discovery document
        # pointing its token_endpoint at cloud metadata).
        for host in ["169.254.169.254", "127.0.0.1", "10.0.0.5", "::1"]:
            with self.assertRaises(OSError, msg=host):
                self._resolve(host)

    def test_public_ip_literal_passes(self):
        self.assertEqual(self._resolve("93.184.216.34")[0]["host"], "93.184.216.34")

    def test_allow_private_passes_internal_literal(self):
        result = self._resolve("10.0.0.5", allow_private=True)
        self.assertEqual(result[0]["host"], "10.0.0.5")

    def test_exempt_address_passes_only_on_its_port(self):
        exempt = frozenset({("10.0.0.1", 3128)})
        self.assertEqual(
            self._resolve("10.0.0.1", 3128, exempt=exempt)[0]["port"], 3128
        )
        with self.assertRaises(OSError):
            self._resolve("10.0.0.1", 22, exempt=exempt)


class SafeSessionTests(SimpleTestCase):
    """Drive real requests at a local listener: a refused connection must not
    reach the socket at all."""

    def _get(self, url_template, *, resolve_to=None, **session_kwargs):
        connections = 0

        async def handle(reader, writer):
            nonlocal connections
            connections += 1
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        async def run():
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                async with (
                    safe_session(**session_kwargs) as session,
                    session.get(url_template.format(port=port)) as res,
                ):
                    return res.status
            finally:
                server.close()
                await server.wait_closed()

        try:
            if resolve_to:
                with resolves_to(resolve_to):
                    return async_to_sync(run)(), connections
            return async_to_sync(run)(), connections
        except aiohttp.ClientError as exc:
            return exc, connections

    def test_listener_reachable_when_private_allowed(self):
        # Control: proves the listener works, so the refusals below are real.
        result, connections = self._get("http://127.0.0.1:{port}/", allow_private=True)
        self.assertEqual((result, connections), (204, 1))

    def test_ip_literal_refused_before_connect(self):
        result, connections = self._get("http://127.0.0.1:{port}/")
        self.assertIsInstance(result, aiohttp.ClientConnectorError)
        self.assertEqual(connections, 0)

    def test_hostname_resolving_internal_refused_before_connect(self):
        result, connections = self._get(
            "http://rebind.example:{port}/", resolve_to="127.0.0.1"
        )
        self.assertIsInstance(result, aiohttp.ClientConnectorError)
        self.assertEqual(connections, 0)

    def test_env_proxy_exempt_with_trust_env(self):
        async def run(**kwargs):
            async with safe_session(**kwargs) as session:
                return session.connector._exempt

        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://10.0.0.1:3128"}):
            self.assertIn(("10.0.0.1", 3128), async_to_sync(run)(trust_env=True))
            self.assertEqual(async_to_sync(run)(trust_env=False), frozenset())


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


# Every ".example" host here is pinned to a public address by ``resolves_to``.
# Relying on the reserved TLD not resolving would hand the outcome to whatever
# the machine's resolver does with an unknown name.
class RequestWithValidatedRedirectsTests(SimpleTestCase):
    def _run(self, session, method, url, **kwargs):
        with resolves_to():
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


class SessionKwargsTests(SimpleTestCase):
    def test_default_timeout_and_override(self):
        # The 30s default lives in session_kwargs, not AIOHTTP_CONFIG; dropping
        # it would silently give every session aiohttp's 5-minute default.
        self.assertEqual(session_kwargs()["timeout"].total, settings.AIOHTTP_TIMEOUT)
        override = aiohttp.ClientTimeout(total=3)
        self.assertIs(session_kwargs(timeout=override)["timeout"], override)


# Boots the app with self-monitoring on, loads what a running process imports
# soon after, and reports whether aiohttp came along at each of two points.
_AIOHTTP_PROBE = """\
import importlib, json, sys, django
django.setup()
for mod in (
    "glitchtip.asgi",
    "glitchtip.api.api",
    "apps.alerts.tasks",
    "apps.organizations_ext.tasks",
    "apps.uptime.tasks",
    "apps.stripe.views",
):
    importlib.import_module(mod)
stages = {"modules": "aiohttp" in sys.modules}
from django.urls import get_resolver
get_resolver().url_patterns
from allauth.account.adapter import get_adapter
get_adapter()
stages["urlconf"] = "aiohttp" in sys.modules
print(json.dumps(stages))
"""


class AiohttpLazyImportTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        result = subprocess.run(
            [sys.executable, "-c", _AIOHTTP_PROBE],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            # A DSN makes settings initialise sentry_sdk, which imports every
            # auto-enabling integration it discovers, aiohttp's among them.
            env={
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
                "SENTRY_DSN": "http://key@sentry.invalid/1",
            },
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        cls.stages = json.loads(result.stdout.strip().splitlines()[-1])

    def test_startup_does_not_import_aiohttp(self):
        self.assertFalse(self.stages["modules"])

    # django-allauth-async imports aiohttp at module level once the URLconf and
    # adapter load, which the first request does. Drop this decorator when it
    # imports lazily; the unexpected success is the reminder.
    @unittest.expectedFailure
    def test_first_request_does_not_import_aiohttp(self):
        self.assertFalse(self.stages["urlconf"])
