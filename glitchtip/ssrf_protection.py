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

Importing this module imports aiohttp, which costs several MiB per process. An
install that never makes an outbound request shouldn't pay that, so code that
runs at startup imports this module and aiohttp inside the function that sends
the request, not at module level.
"""

import asyncio
import contextlib
import logging
import socket

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.helpers import proxies_from_env
from aiohttp.resolver import ThreadedResolver
from django.conf import settings
from yarl import URL

from glitchtip.url_validation import check_url_safe, is_ip_blocked, is_ip_literal

logger = logging.getLogger(__name__)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 8


class BlockedRedirectError(aiohttp.ClientError):
    """Raised when an outbound request is steered at a blocked/internal target."""


def _address_blocked(ip_str: str) -> bool:
    try:
        return is_ip_blocked(ip_str)
    except ValueError:
        # Not an IP literal (shouldn't happen for a resolver result): refuse
        # what we can't judge.
        return True


async def resolve_validated_ip(host: str, allow_private: bool = False) -> str:
    """Resolve `host` and return a single validated IP to connect to.

    For non-aiohttp connects (raw TCP, e.g. uptime PORT checks) that would
    otherwise re-resolve DNS at connect time and reopen the rebinding window.
    Pin the returned literal IP as the connect target so validation and connect
    see the same address. Raises BlockedRedirectError if the host cannot be
    resolved or resolves only to blocked addresses.
    """
    if is_ip_literal(host):
        if allow_private or not _address_blocked(host):
            return host
        raise BlockedRedirectError(f"blocked internal address: {host}")

    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, None)
    except OSError as exc:
        raise BlockedRedirectError(f"cannot resolve host: {host}") from exc

    for info in infos:
        ip = info[4][0]
        if allow_private or not _address_blocked(ip):
            return ip
    raise BlockedRedirectError(f"host resolves only to internal addresses: {host}")


class ValidatingResolver(AbstractResolver):
    """aiohttp resolver that refuses to return private/internal addresses.

    aiohttp dials whatever this returns, so filtering here pins the safety check
    to the address actually connected to and eliminates the DNS-rebinding window
    between a pre-flight ``getaddrinfo`` and the real connect.

    ``exempt`` holds ``(host, port)`` pairs that are always allowed: the
    configured outbound proxy, which is often on a private network.
    """

    def __init__(self, allow_private: bool = False, exempt: frozenset = frozenset()):
        self._allow_private = allow_private
        self._exempt = exempt
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
        if self._allow_private or (host, port) in self._exempt:
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


class ValidatingConnector(aiohttp.TCPConnector):
    """TCPConnector that refuses to dial private/internal addresses.

    Hostnames are vetted by :class:`ValidatingResolver`. IP-literal hosts never
    reach a resolver -- ``TCPConnector._resolve_host`` short-circuits them -- so
    a URL, redirect, or OIDC discovery document naming
    ``https://169.254.169.254/`` is refused here instead.

    Through a proxy (``trust_env`` with ``HTTP(S)_PROXY`` set) the connector
    only ever dials the proxy, so it vets the proxy, not the target; ``exempt``
    lets that proxy through. The target is then the proxy's egress policy to
    enforce -- callers that pre-validate (``check_url_safe``, as webhooks and
    uptime do) still check it, but without the connect-time rebinding guarantee.
    """

    def __init__(
        self, allow_private: bool = False, exempt: frozenset = frozenset(), **kwargs
    ):
        super().__init__(resolver=ValidatingResolver(allow_private, exempt), **kwargs)
        self._allow_private = allow_private
        self._exempt = exempt
        # TCPConnector treats a passed-in resolver as externally owned and
        # never closes it; this one is ours, so close it with the connector.
        self._resolver_owner = True

    async def _resolve_host(self, host, port, traces=None):
        if (
            not self._allow_private
            and (host, port) not in self._exempt
            and is_ip_literal(host)
            and _address_blocked(host)
        ):
            logger.warning("blocked outbound connection to internal address %r", host)
            raise OSError(f"URL host {host!r} is a private/internal address")
        return await super()._resolve_host(host, port, traces)


def _env_proxy_addresses() -> frozenset:
    """``(host, port)`` of each proxy aiohttp takes from the environment."""
    return frozenset(
        (info.proxy.host, info.proxy.port) for info in proxies_from_env().values()
    )


def session_kwargs(**overrides) -> dict:
    """``ClientSession`` keyword arguments: ``AIOHTTP_CONFIG`` plus the default
    ``AIOHTTP_TIMEOUT``, with ``overrides`` taking precedence."""
    return {
        "timeout": aiohttp.ClientTimeout(total=settings.AIOHTTP_TIMEOUT),
        **settings.AIOHTTP_CONFIG,
        **overrides,
    }


def safe_session(allow_private: bool = False, **kwargs) -> aiohttp.ClientSession:
    """A ClientSession whose connector refuses to dial internal addresses.

    Uses the project-wide :func:`session_kwargs` (timeout, user-agent, etc.) and
    owns a fresh connector per session so its lifecycle matches the session's.
    """
    kwargs = session_kwargs(**kwargs)
    exempt = _env_proxy_addresses() if kwargs.get("trust_env") else frozenset()
    connector = ValidatingConnector(allow_private, exempt)
    return aiohttp.ClientSession(connector=connector, **kwargs)


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

    The `timeout` (a ClientTimeout) bounds the whole redirect chain, matching
    aiohttp's built-in auto-follow semantics, rather than resetting per hop.
    """
    kwargs.pop("allow_redirects", None)
    timeout = kwargs.get("timeout")
    overall = timeout.total if isinstance(timeout, aiohttp.ClientTimeout) else None
    deadline = asyncio.timeout(overall) if overall else contextlib.nullcontext()

    async with deadline:
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
                # Per RFC 7231, 301/302/303 turn a non-GET/HEAD into GET.
                if status in (301, 302, 303) and method.upper() not in ("GET", "HEAD"):
                    method = "GET"
                continue
            return response
        raise BlockedRedirectError(f"too many redirects following {url}")
