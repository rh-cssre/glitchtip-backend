import logging

from django.test import SimpleTestCase

from glitchtip.proxy_headers import ProxyHeaders

# Real public addresses: documentation ranges such as 203.0.113.0/24 are not
# global, so they would count as trusted proxies.
CLIENT = "81.2.69.142"
FORGED = "6.6.6.6"
CDN_EDGE = "104.16.0.1"
CDN_RANGE = "104.16.0.0/13"


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
        cases = [
            # (label, TRUSTED_PROXIES, peer, headers, expected client)
            ("direct, no header", None, CLIENT, [], CLIENT),
            ("private proxy", None, "10.0.0.9", xff(CLIENT), CLIENT),
            (
                "forged prefix behind a proxy",
                None,
                "10.0.0.9",
                xff(f"{FORGED}, {CLIENT}"),
                CLIENT,
            ),
            (
                "forged private prefix behind a proxy",
                None,
                "10.0.0.9",
                xff(f"10.9.9.9, {CLIENT}"),
                CLIENT,
            ),
            ("public peer is not trusted", None, CDN_EDGE, xff(FORGED), CDN_EDGE),
            ("CGNAT proxy", None, "100.64.3.3", xff(CLIENT), CLIENT),
            ("IPv6 ULA proxy", None, "fd00::1", xff("2a00:1450::1"), "2a00:1450::1"),
            (
                "LAN client keeps its address",
                None,
                "172.17.0.1",
                xff("192.168.1.50"),
                "192.168.1.50",
            ),
            (
                "listed CDN as the peer",
                CDN_RANGE,
                CDN_EDGE,
                xff(f"{FORGED}, {CLIENT}"),
                CLIENT,
            ),
            (
                "listed CDN behind a private proxy",
                CDN_RANGE,
                "10.0.0.9",
                xff(f"{FORGED}, {CLIENT}, {CDN_EDGE}"),
                CLIENT,
            ),
            (
                "unlisted CDN behind a private proxy",
                None,
                "10.0.0.9",
                xff(f"{CLIENT}, {CDN_EDGE}"),
                CDN_EDGE,
            ),
            (
                "repeated headers are joined in order",
                None,
                "10.0.0.9",
                [("x-forwarded-for", CLIENT), ("x-forwarded-for", "10.0.0.7")],
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
            ("* takes the leftmost", "*", CDN_EDGE, xff(f"{FORGED}, {CLIENT}"), FORGED),
        ]
        for label, trusted, peer, headers, expected in cases:
            with self.subTest(label):
                scope = await resolve(trusted, peer, headers)
                self.assertEqual(scope["client"][0], expected)

    async def test_scheme_is_honoured_from_any_peer(self):
        """A proxy that isn't trusted for addresses still sets https.

        Missing https breaks CSRF on every POST, so this must not depend on
        TRUSTED_PROXIES.
        """
        headers = [("x-forwarded-proto", "https"), ("x-forwarded-for", FORGED)]
        for trusted in (None, "10.0.0.0/8"):
            with self.subTest(trusted=trusted):
                scope = await resolve(trusted, CDN_EDGE, headers)
                self.assertEqual(scope["scheme"], "https")

    def enable_warnings(self):
        # settings.py disables WARNING-and-below while testing.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)

    async def test_untrusted_peer_warns_once(self):
        self.enable_warnings()
        wrapper = ProxyHeaders(echo_app)
        scope = {
            "type": "http",
            "client": (CDN_EDGE, 1),
            "headers": [(b"x-forwarded-for", CLIENT.encode())],
        }
        with self.assertLogs("glitchtip.proxy_headers", "WARNING") as logs:
            await wrapper(dict(scope), None, None)
            await wrapper(dict(scope), None, None)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(CDN_EDGE, logs.output[0])

    def test_invalid_entry_is_skipped(self):
        self.enable_warnings()
        with self.assertLogs("glitchtip.proxy_headers", "WARNING"):
            wrapper = ProxyHeaders(echo_app, f"not-an-ip, {CDN_RANGE}")
        self.assertEqual([str(n) for n in wrapper.networks], [CDN_RANGE])
