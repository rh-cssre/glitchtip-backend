"""Async-cached fetch of OpenID Connect discovery documents.

The django-allauth ``OpenIDConnectOAuth2Adapter`` lazily fetches the
provider's ``.well-known/openid-configuration`` with a synchronous
``requests`` call the first time ``authorize_url`` (or any related
property) is accessed. The unauthenticated ``/api/0/settings/`` endpoint
constructs a fresh adapter per request, so without caching every
visitor with an OIDC SocialApp configured triggers a blocking outbound
HTTP call on a worker thread.

This module fetches the discovery document with ``aiohttp`` and caches
the JSON in the Django cache (Valkey in production), keyed by the
provider's server URL.
"""

import hashlib
import logging
from typing import Any

import aiohttp
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

OIDC_DISCOVERY_CACHE_TTL = 3600
OIDC_DISCOVERY_TIMEOUT = 5


def _cache_key(server_url: str) -> str:
    digest = hashlib.sha256(server_url.encode()).hexdigest()[:32]
    return f"oidc_discovery:{digest}"


async def get_openid_config(server_url: str) -> dict[str, Any] | None:
    """Return the OpenID discovery document, cached on first fetch.

    Returns ``None`` on network or parse error so callers can degrade
    gracefully (the OAuth flow itself will surface a clearer error to the
    user when they attempt to log in).
    """
    key = _cache_key(server_url)
    cached = await cache.aget(key)
    if cached is not None:
        return cached
    timeout = aiohttp.ClientTimeout(total=OIDC_DISCOVERY_TIMEOUT)
    try:
        async with (
            aiohttp.ClientSession(
                timeout=timeout, **settings.AIOHTTP_CONFIG
            ) as session,
            session.get(server_url) as resp,
        ):
            resp.raise_for_status()
            config = await resp.json()
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        logger.warning("OIDC discovery failed for %s: %s", server_url, exc)
        return None
    await cache.aset(key, config, OIDC_DISCOVERY_CACHE_TTL)
    return config


async def get_authorize_url(server_url: str) -> str | None:
    config = await get_openid_config(server_url)
    if config is None:
        return None
    return config.get("authorization_endpoint")
