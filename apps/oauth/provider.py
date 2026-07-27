import hashlib
import hmac

from .models import OAuthRefreshToken

# NOTE: keep this module free of any ``mcp`` import. It is on the default web
# boot path (URL routing -> oauth.views -> here), and importing the MCP SDK
# pulls the FastMCP server stack (uvicorn + sse_starlette + jsonschema,
# ~22 MiB resident) into every web process even when GLITCHTIP_ENABLE_MCP is
# off. The MCP-dependent provider lives in mcp_provider.py instead.

ACCESS_TOKEN_LIFETIME = 28800  # 8 hours
REFRESH_TOKEN_LIFETIME = 30 * 86400  # 30 days
AUTH_CODE_LIFETIME = 300  # 5 minutes

TOKEN_PREFIX_LENGTH = 8

VALID_SCOPES = [
    "project:read",
    "project:write",
    "project:admin",
    "project:releases",
    "team:read",
    "team:write",
    "team:admin",
    "event:read",
    "event:write",
    "event:admin",
    "org:read",
    "org:write",
    "org:admin",
    "member:read",
    "member:write",
    "member:admin",
]

DEFAULT_SCOPES = [
    "project:read",
    "team:read",
    "event:read",
    "event:write",
    "org:read",
    "member:read",
]


def _hash_token(token: str) -> str:
    """Hash a high-entropy OAuth secret for at-rest storage.

    SHA-256 is appropriate here because inputs are 256-bit random strings
    from ``generate_token()``; a password-style KDF adds no value.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _token_prefix(token: str) -> str:
    return token[:TOKEN_PREFIX_LENGTH]


def _grant_cache_key(code: str) -> str:
    return f"oauth_grant:{_hash_token(code)}"


def _access_cache_key_from_digest(digest: str) -> str:
    return f"oauth_access:{digest}"


def _access_cache_key(token: str) -> str:
    return _access_cache_key_from_digest(_hash_token(token))


async def _find_refresh_token(
    plaintext: str, **extra_filters
) -> OAuthRefreshToken | None:
    """Look up a refresh token by prefix, verify via constant-time digest compare."""
    prefix = _token_prefix(plaintext)
    expected = _hash_token(plaintext)
    async for rt in OAuthRefreshToken.objects.filter(
        token_prefix=prefix, **extra_filters
    ):
        if hmac.compare_digest(rt.token_digest, expected):
            return rt
    return None
