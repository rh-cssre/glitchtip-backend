"""Connect-time SSRF protection for outbound HTTP to user-supplied URLs.

`glitchtip.url_validation` validates a *submitted* URL before a request is made.
That is necessary but not sufficient: an aiohttp client re-resolves DNS at
connect time and follows redirects, so a URL that validated as public can still
be steered to an internal address by

  * a redirect (``302 Location: http://169.254.169.254/``), whose target is
    never re-validated, or
  * DNS rebinding, where the name resolves to a public IP during validation and
    to an internal IP microseconds later at connect time.

This module closes both gaps:

  * :class:`ValidatingResolver` refuses to return blocked addresses, so aiohttp
    connects only to an address that has been validated at the moment of the
    dial -- there is no window between validation and connect.
  * :func:`request_with_validated_redirects` disables aiohttp's automatic
    redirect following and re-validates every hop (including redirects to raw
    IP literals, which bypass the resolver) before dialing it.

Callers that never legitimately need to follow a cross-host redirect (webhook
delivery) can instead just pass ``allow_redirects=False`` and rely on the
resolver; callers that do (uptime monitoring of URLs that 301 http->https) use
:func:`request_with_validated_redirects`.
"""

import logging
import socket

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from django.conf import settings
from yarl import URL

from glitchtip.url_validation import check_url_safe, is_ip_blocked

logger = logging.getLogger(__name__)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 8


class BlockedRedirectError(aiohttp.ClientError):
    """Raised when an outbound request is steered at a blocked/internal target."""


def _address_blocked(ip_str: str) -> bool:
    try:
        return is_ip_blocked(ip_str)
    except ValueError:
        # Not an IP literal (shouldn't happen for a resolver result); be safe.
        return False


class ValidatingResolver(AbstractResolver):
    """aiohttp resolver that refuses to return private/internal addresses.

    aiohttp dials whatever this returns, so filtering here pins the safety check
    to the address actually connected to and eliminates the DNS-rebinding window
    between a pre-flight ``getaddrinfo`` and the real connect.
    """

    def __init__(self, allow_private: bool = False):
        self._allow_private = allow_private
        # Created lazily: ThreadedResolver binds to the running loop, which does
        # not exist yet when a connector is built outside an async context.
        self._resolver: ThreadedResolver | None = None

    def _inner(self) -> ThreadedResolver:
        if self._resolver is None:
            self._resolver = ThreadedResolver()
        return self._resolver

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict]:
        hosts = await self._inner().resolve(host, port, family)
        if self._allow_private:
            return hosts
        allowed = [h for h in hosts if not _address_blocked(h["host"])]
        if not allowed:
            logger.warning(
                "blocked outbound connection: %r resolves only to internal addresses",
                host,
            )
            raise OSError(f"URL host {host!r} resolves to a private/internal address")
        return allowed

    async def close(self) -> None:
        if self._resolver is not None:
            await self._resolver.close()


def safe_session(allow_private: bool = False, **kwargs) -> aiohttp.ClientSession:
    """A ClientSession whose connector refuses to dial internal addresses.

    Merges the project-wide ``AIOHTTP_CONFIG`` (timeout, user-agent, etc.) and
    owns a fresh connector per session so its lifecycle matches the session's.
    """
    connector = aiohttp.TCPConnector(resolver=ValidatingResolver(allow_private))
    return aiohttp.ClientSession(
        connector=connector, **{**settings.AIOHTTP_CONFIG, **kwargs}
    )


async def request_with_validated_redirects(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    allow_private: bool = False,
    max_redirects: int = MAX_REDIRECTS,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Perform ``method`` against ``url``, validating every redirect hop.

    aiohttp's automatic redirect following is disabled; each hop's target is
    re-validated with :func:`check_url_safe` (which rejects both blocked
    hostnames and raw IP literals) before it is dialed. Returns the final
    (non-redirect) response, which the caller owns and must release/close.
    """
    kwargs.pop("allow_redirects", None)
    current = URL(url)

    for _ in range(max_redirects + 1):
        if not await check_url_safe(str(current), allow_private):
            raise BlockedRedirectError(
                f"blocked SSRF target (private/internal IP): {current}"
            )
        response = await session.request(
            method, current, allow_redirects=False, **kwargs
        )
        if response.status in REDIRECT_STATUSES and "Location" in response.headers:
            location = response.headers["Location"]
            status = response.status
            await response.release()
            current = current.join(URL(location))
            # Per RFC 7231, 301/302/303 turn a non-GET/HEAD into GET on follow.
            if status in (301, 302, 303) and method.upper() not in ("GET", "HEAD"):
                method = "GET"
            continue
        return response

    raise BlockedRedirectError(f"too many redirects following {url}")
