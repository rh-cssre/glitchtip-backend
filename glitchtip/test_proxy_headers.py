import logging
import os
from unittest import mock

from django.test import SimpleTestCase

from glitchtip.proxy_headers import ProxyHeaders

# Real public addresses: documentation ranges such as 203.0.113.0/24 are not
# global, which would hide mistakes in what counts as private.
CLIENT = "81.2.69.142"
FORGED = "6.6.6.6"
CDN_EDGE = "104.16.0.1"  # Cloudflare


async def echo_app(scope, receive, send):
    return scope


async def resolve(trusted_proxies, peer, headers):
    scope = {
        "type": "http",
        "scheme": "http",
        "client": (peer, 1234) if peer else None,
        "headers": [(name.encode(), value.encode()) for name, value in headers],
    }
    return await ProxyHeaders(echo_app, trusted_proxies)(scope, None, None)


def xff(value):
    return [("x-forwarded-for", value)]


class ProxyHeadersTestCase(SimpleTestCase):
    async def test_client_address(self):
        private_chain = ", ".join(f"10.0.0.{i}" for i in range(1, 21))
        cases = [
            # (label, TRUSTED_PROXIES, peer, headers, expected client)
            ("direct, no header", None, CLIENT, [], CLIENT),
            ("private proxy", None, "10.0.0.9", xff(CLIENT), CLIENT),
            ("forged prefix", None, "10.0.0.9", xff(f"{FORGED}, {CLIENT}"), CLIENT),
            (
                "forged private prefix",
                None,
                "10.0.0.9",
                xff(f"10.9.9.9, {CLIENT}"),
                CLIENT,
            ),
            ("public peer is not trusted", None, CDN_EDGE, xff(FORGED), CDN_EDGE),
            ("CGNAT proxy", None, "100.64.3.3", xff(CLIENT), CLIENT),
            ("IPv6 ULA proxy", None, "fd00::1", xff("2a00:1450::1"), "2a00:1450::1"),
            ("IPv4-mapped peer", None, "::ffff:10.0.0.9", xff(CLIENT), CLIENT),
            (
                "Teredo peer is public",
                None,
                "2001:0:4136::1",
                xff(FORGED),
                "2001:0:4136::1",
            ),
            ("LAN client", None, "172.17.0.1", xff("192.168.1.50"), "192.168.1.50"),
            ("unix socket", None, None, xff(CLIENT), CLIENT),
            (
                "unlisted CDN behind a private proxy",
                None,
                "10.0.0.9",
                xff(f"{CLIENT}, {CDN_EDGE}"),
                CDN_EDGE,
            ),
            (
                "cloudflare keyword",
                "cloudflare,private",
                "10.0.0.9",
                xff(f"{FORGED}, {CLIENT}, {CDN_EDGE}"),
                CLIENT,
            ),
            ("listed CDN as the peer", "cloudflare", CDN_EDGE, xff(CLIENT), CLIENT),
            (
                "a set value replaces private",
                "cloudflare",
                "10.0.0.9",
                xff(CLIENT),
                "10.0.0.9",
            ),
            (
                "loopback is always trusted",
                "cloudflare",
                "127.0.0.1",
                xff(CLIENT),
                CLIENT,
            ),
            (
                "repeated headers are joined in order",
                None,
                "10.0.0.9",
                [("x-forwarded-for", FORGED), ("x-forwarded-for", CLIENT)],
                CLIENT,
            ),
            ("port is removed", None, "10.0.0.9", xff(f"{CLIENT}:5678"), CLIENT),
            (
                "bracketed IPv6 port is removed",
                None,
                "10.0.0.9",
                xff("[2a00:1450::1]:443"),
                "2a00:1450::1",
            ),
            (
                "a hop that is not an address",
                None,
                "10.0.0.9",
                xff("unknown"),
                "10.0.0.9",
            ),
            (
                "the last trusted hop before one that is not an address",
                None,
                None,
                xff(f"{CLIENT}, unknown, 10.0.0.7"),
                "10.0.0.7",
            ),
            (
                "only the last 16 hops are examined",
                None,
                "10.0.0.99",
                xff(f"{CLIENT}, {private_chain}"),
                "10.0.0.5",
            ),
            ("* takes the leftmost", "*", CDN_EDGE, xff(f"{FORGED}, {CLIENT}"), FORGED),
        ]
        for label, trusted, peer, headers, expected in cases:
            with self.subTest(label):
                scope = await resolve(trusted, peer, headers)
                self.assertEqual(scope["client"][0], expected)

    async def test_scheme_is_honoured_from_any_peer(self):
        """An untrusted proxy still sets https.

        Missing https breaks CSRF on every POST, so this must not depend on
        TRUSTED_PROXIES.
        """
        for proto in ("https", "HTTPS, http"):
            with self.subTest(proto):
                headers = [("x-forwarded-proto", proto)]
                scope = await resolve(None, CDN_EDGE, headers)
                self.assertEqual(scope["scheme"], "https")

    async def test_lifespan_passes_through(self):
        scope = {"type": "lifespan"}
        self.assertIs(await ProxyHeaders(echo_app)(scope, None, None), scope)

    def enable_logging(self):
        # settings.py disables WARNING-and-below while testing.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)

    async def test_logs_what_to_set(self):
        self.enable_logging()
        wrapper = ProxyHeaders(echo_app)

        async def request(peer, forwarded_for):
            scope = {
                "type": "http",
                "client": (peer, 1),
                "headers": [(b"x-forwarded-for", forwarded_for.encode())],
            }
            await wrapper(scope, None, None)

        with (
            mock.patch.dict(os.environ),
            self.assertLogs("glitchtip.proxy_headers", "INFO") as logs,
        ):
            os.environ.pop("KUBERNETES_SERVICE_HOST", None)
            for _ in range(2):
                await request(CDN_EDGE, CLIENT)
                await request("10.0.0.9", f"{CLIENT}, {CDN_EDGE}")
        self.assertEqual(len(logs.records), 3, logs.output)
        untrusted, cloudflare, seen = logs.output
        self.assertIn(f"Ignoring X-Forwarded-For from {CDN_EDGE}", untrusted)
        self.assertIn("Add cloudflare to TRUSTED_PROXIES", cloudflare)
        self.assertIn("Trusted proxies seen so far: 10.0.0.9.", seen)
        self.assertIn("set TRUSTED_PROXIES to their addresses", seen)

    async def test_kubernetes_hint_only_when_unset(self):
        self.enable_logging()
        scope = {
            "type": "http",
            "client": ("10.0.0.9", 1),
            "headers": [(b"x-forwarded-for", CLIENT.encode())],
        }
        for setting, expect_hint in ((None, True), ("10.0.0.0/8", False)):
            with self.subTest(setting=setting):
                wrapper = ProxyHeaders(echo_app, setting)
                env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}
                with (
                    mock.patch.dict(os.environ, env),
                    self.assertLogs("glitchtip.proxy_headers", "INFO") as logs,
                ):
                    await wrapper(dict(scope), None, None)
                hint = "set TRUSTED_PROXIES to the network range"
                self.assertEqual(hint in logs.output[0], expect_hint)

    def test_invalid_entry_is_skipped(self):
        self.enable_logging()
        with self.assertLogs("glitchtip.proxy_headers", "WARNING"):
            wrapper = ProxyHeaders(echo_app, f"not-an-ip, {CDN_EDGE}")
        self.assertTrue(wrapper.is_trusted_peer(CDN_EDGE))
