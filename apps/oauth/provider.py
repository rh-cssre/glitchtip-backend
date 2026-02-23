import json
import time

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from apps.api_tokens.models import generate_token

from .models import OAuthApplication, OAuthRefreshToken

ACCESS_TOKEN_LIFETIME = 3600  # 1 hour
REFRESH_TOKEN_LIFETIME = 30 * 86400  # 30 days
AUTH_CODE_LIFETIME = 300  # 5 minutes

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
    "org:read",
    "member:read",
]


def _grant_cache_key(code: str) -> str:
    return f"oauth_grant:{code}"


def _access_cache_key(token: str) -> str:
    return f"oauth_access:{token}"


class GlitchTipOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        try:
            app = await OAuthApplication.objects.aget(client_id=client_id)
        except OAuthApplication.DoesNotExist:
            return None
        if app.client_secret_expires_at and app.client_secret_expires_at < int(
            time.time()
        ):
            return None
        return OAuthClientInformationFull(**app.client_info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await OAuthApplication.objects.acreate(
            client_id=client_info.client_id,
            client_secret=client_info.client_secret or "",
            client_id_issued_at=client_info.client_id_issued_at or int(time.time()),
            client_secret_expires_at=client_info.client_secret_expires_at,
            client_info=json.loads(client_info.model_dump_json()),
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        # If no scopes requested, fall back to client's registered scopes
        scopes = params.scopes
        if not scopes and client.scope:
            scopes = client.scope.split()

        data = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": scopes,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        signed = signing.dumps(data)
        base_url = settings.GLITCHTIP_URL.geturl()
        return f"{base_url}/oauth/authorize/?data={signed}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        key = _grant_cache_key(authorization_code)
        data = await cache.aget(key)
        if data is None:
            return None
        grant = json.loads(data)
        if grant["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=grant["scopes"],
            expires_at=grant["expires_at"],
            client_id=grant["client_id"],
            code_challenge=grant["code_challenge"],
            redirect_uri=grant["redirect_uri"],
            redirect_uri_provided_explicitly=grant["redirect_uri_provided_explicitly"],
            resource=grant.get("resource"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        grant_key = _grant_cache_key(authorization_code.code)

        # Read the full grant data (includes user_id) before deleting
        raw = await cache.aget(grant_key)
        if raw is None:
            # Grant already consumed or expired
            from mcp.server.auth.provider import TokenError

            raise TokenError(
                error="invalid_grant", error_description="Authorization code not found"
            )
        grant = json.loads(raw)
        user_id = grant["user_id"]

        # Delete the grant (one-time use)
        await cache.adelete(grant_key)

        # Generate access token and store in cache
        now = int(time.time())
        access_token_str = generate_token()
        access_data = json.dumps(
            {
                "user_id": user_id,
                "client_id": client.client_id,
                "scopes": authorization_code.scopes,
                "expires_at": now + ACCESS_TOKEN_LIFETIME,
                "resource": authorization_code.resource,
            }
        )
        await cache.aset(
            _access_cache_key(access_token_str),
            access_data,
            ACCESS_TOKEN_LIFETIME,
        )

        # Create refresh token in DB
        refresh_token_str = generate_token()
        await OAuthRefreshToken.objects.acreate(
            token=refresh_token_str,
            application_id=client.client_id,
            user_id=user_id,
            access_token_key=access_token_str,
            scopes=" ".join(authorization_code.scopes),
            expires_at=now + REFRESH_TOKEN_LIFETIME,
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_LIFETIME,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh_token_str,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        try:
            rt = await OAuthRefreshToken.objects.aget(
                token=refresh_token,
                application_id=client.client_id,
                is_revoked=False,
            )
        except OAuthRefreshToken.DoesNotExist:
            return None
        if rt.expires_at and rt.expires_at < int(time.time()):
            return None
        return RefreshToken(
            token=rt.token,
            client_id=rt.application_id,
            scopes=rt.scopes.split() if rt.scopes else [],
            expires_at=rt.expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        old_rt = await OAuthRefreshToken.objects.aget(
            token=refresh_token.token, is_revoked=False
        )

        # Revoke old refresh token and delete old access token from cache
        old_rt.is_revoked = True
        await old_rt.asave(update_fields=["is_revoked"])
        await cache.adelete(_access_cache_key(old_rt.access_token_key))

        # Create new access token in cache
        now = int(time.time())
        new_access_token_str = generate_token()
        effective_scopes = scopes if scopes else refresh_token.scopes
        access_data = json.dumps(
            {
                "user_id": old_rt.user_id,
                "client_id": client.client_id,
                "scopes": effective_scopes,
                "expires_at": now + ACCESS_TOKEN_LIFETIME,
                "resource": None,
            }
        )
        await cache.aset(
            _access_cache_key(new_access_token_str),
            access_data,
            ACCESS_TOKEN_LIFETIME,
        )

        # Create new refresh token in DB
        new_refresh_token_str = generate_token()
        await OAuthRefreshToken.objects.acreate(
            token=new_refresh_token_str,
            application_id=client.client_id,
            user_id=old_rt.user_id,
            access_token_key=new_access_token_str,
            scopes=" ".join(effective_scopes),
            expires_at=now + REFRESH_TOKEN_LIFETIME,
        )

        return OAuthToken(
            access_token=new_access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_LIFETIME,
            scope=" ".join(effective_scopes),
            refresh_token=new_refresh_token_str,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Check cache for OAuth access token
        data = await cache.aget(_access_cache_key(token))
        if data is not None:
            parsed = json.loads(data)
            now = int(time.time())
            if parsed["expires_at"] > now:
                return AccessToken(
                    token=token,
                    client_id=str(parsed["user_id"]),
                    scopes=parsed["scopes"],
                    expires_at=parsed["expires_at"],
                    resource=parsed.get("resource"),
                )
            await cache.adelete(_access_cache_key(token))

        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await cache.adelete(_access_cache_key(token.token))
            async for rt in OAuthRefreshToken.objects.filter(
                access_token_key=token.token, is_revoked=False
            ):
                rt.is_revoked = True
                await rt.asave(update_fields=["is_revoked"])
        elif isinstance(token, RefreshToken):
            try:
                rt = await OAuthRefreshToken.objects.aget(
                    token=token.token, is_revoked=False
                )
                rt.is_revoked = True
                await rt.asave(update_fields=["is_revoked"])
                await cache.adelete(_access_cache_key(rt.access_token_key))
            except OAuthRefreshToken.DoesNotExist:
                pass
