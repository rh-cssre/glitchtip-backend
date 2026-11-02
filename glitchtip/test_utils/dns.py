"""Deterministic DNS for tests that exercise the SSRF guards.

``check_url_safe`` and ``resolve_validated_ip`` resolve every hostname handed to
them, so any test that drives them through a named host depends on what the
machine's resolver says about that name. Reserved ``.example`` names look safe
to rely on -- RFC 2606 says they never resolve -- but a resolver that answers an
unknown name with an address (wildcard DNS, a captive portal, some CI networks)
turns that assumption into a test failure with no relationship to the code under
test.

Pin the answer instead. ``resolves_to`` also makes the guards take their real
path -- resolve, then judge the address -- rather than the DNS-failed shortcut
that a non-resolving name happens to take.
"""

import contextlib
import socket
from unittest import mock

# A routable public address, so is_ip_blocked() lets it through. Deliberately
# not an RFC 5737 documentation range (192.0.2/24, 198.51.100/24, 203.0.113/24):
# Python's ipaddress marks those is_private, so the guards would block them.
PUBLIC_IP = "93.184.216.34"


@contextlib.contextmanager
def resolves_to(ip: str = PUBLIC_IP):
    """Resolve every hostname to `ip` for the duration of the block."""

    async def fake_getaddrinfo(_loop, _host, port=0, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    with mock.patch("asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True):
        yield


@contextlib.contextmanager
def resolution_fails():
    """Fail every hostname lookup for the duration of the block."""

    async def fake_getaddrinfo(*_, **__):
        raise socket.gaierror("resolution disabled for this test")

    with mock.patch("asyncio.BaseEventLoop.getaddrinfo", fake_getaddrinfo, create=True):
        yield
