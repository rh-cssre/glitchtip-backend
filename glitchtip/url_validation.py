"""Shared URL / IP validation helpers for outbound HTTP callers.

These exist to prevent SSRF against internal infrastructure (cloud instance
metadata, loopback services, RFC1918 ranges) from server-side fetches
triggered by user-supplied URLs — webhooks, uptime monitors, etc.

Each caller decides whether private IPs are allowed by passing `allow_private`.
The webhook and uptime subsystems have separate config flags
(`GLITCHTIP_ALLOW_PRIVATE_IPS`, `GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS`) so an
operator can enable private-IP targets for uptime monitoring without also
opening an exfiltration channel through webhook alerts.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_IP_MESSAGE = "URLs targeting private or internal IPs are not allowed"


def is_ip_blocked(ip_str: str) -> bool:
    """Return True if `ip_str` is private, loopback, link-local, reserved, or multicast.

    Raises ValueError if `ip_str` is not a valid IP address literal.
    """
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    )


def _parse_hostname(url: str) -> str | None:
    if "://" in url:
        return urlparse(url).hostname
    # host[:port] form (uptime PORT monitors)
    return url.split(":")[0] or None


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


async def check_url_safe(url: str, allow_private: bool = False) -> bool:
    """Async: resolve hostname and return False if it targets a blocked IP.

    Always returns True when `allow_private` is True. DNS failures return True
    because the subsequent fetch will also fail cleanly.
    """
    if allow_private:
        return True

    hostname = _parse_hostname(url)
    if not hostname:
        return False

    if _is_ip_literal(hostname):
        return not is_ip_blocked(hostname)

    try:
        infos = await asyncio.get_event_loop().getaddrinfo(hostname, None)
    except OSError:
        return True

    for info in infos:
        if is_ip_blocked(info[4][0]):
            return False
    return True


def validate_public_url(url: str, allow_private: bool = False) -> None:
    """Sync validator for schema-layer input. Raises ValueError if URL is blocked.

    DNS lookup failures are treated as non-blocking — the async runtime check
    inside the request handler will catch any actual rebind.
    """
    if allow_private:
        return

    hostname = _parse_hostname(url)
    if not hostname:
        raise ValueError("URL has no hostname")

    if _is_ip_literal(hostname):
        if is_ip_blocked(hostname):
            raise ValueError(BLOCKED_IP_MESSAGE)
        return

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return

    for info in infos:
        if is_ip_blocked(info[4][0]):
            raise ValueError(BLOCKED_IP_MESSAGE)
