import json
import time

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import TestCase, override_settings
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from model_bakery import baker

from apps.api_tokens.models import APIToken, generate_token
from apps.oauth.models import OAuthApplication, OAuthRefreshToken
from apps.oauth.provider import (
    ACCESS_TOKEN_LIFETIME,
    AUTH_CODE_LIFETIME,
    GlitchTipOAuthProvider,
    _access_cache_key,
    _grant_cache_key,
)


def _make_client_info(**overrides):
    defaults = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": None,
        "redirect_uris": ["http://localhost:3000/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "org:read event:read",
    }
    defaults.update(overrides)
    return OAuthClientInformationFull(**defaults)


class OAuthProviderGetClientTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()

    async def test_get_client_not_found(self):
        result = await self.provider.get_client("nonexistent")
        self.assertIsNone(result)

    async def test_register_and_get_client(self):
        info = _make_client_info()
        await self.provider.register_client(info)
        result = await self.provider.get_client("test-client-id")
        self.assertIsNotNone(result)
        self.assertEqual(result.client_id, "test-client-id")
        self.assertEqual(result.client_secret, "test-client-secret")

    async def test_expired_client_secret_returns_none(self):
        info = _make_client_info(client_secret_expires_at=int(time.time()) - 100)
        await self.provider.register_client(info)
        result = await self.provider.get_client("test-client-id")
        self.assertIsNone(result)


@override_settings(
    GLITCHTIP_URL=type("URL", (), {"geturl": lambda self: "http://localhost:8000"})()
)
class OAuthProviderAuthorizeTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()

    async def test_authorize_returns_consent_url(self):
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyUrl

        info = _make_client_info()
        params = AuthorizationParams(
            state="test-state",
            scopes=["org:read"],
            code_challenge="challenge123",
            redirect_uri=AnyUrl("http://localhost:3000/callback"),
            redirect_uri_provided_explicitly=True,
        )
        url = await self.provider.authorize(info, params)
        self.assertIn("/oauth/authorize/", url)
        self.assertIn("data=", url)


class OAuthProviderAuthCodeFlowTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()
        self.user = baker.make("users.user", is_active=True)

    def tearDown(self):
        cache.clear()

    async def test_load_authorization_code_not_found(self):
        info = _make_client_info()
        result = await self.provider.load_authorization_code(info, "nonexistent")
        self.assertIsNone(result)

    async def test_load_authorization_code_wrong_client(self):
        info = _make_client_info()
        code = generate_token()
        grant_data = json.dumps(
            {
                "client_id": "other-client",
                "user_id": self.user.id,
                "scopes": ["org:read"],
                "expires_at": int(time.time()) + AUTH_CODE_LIFETIME,
                "code_challenge": "challenge",
                "redirect_uri": "http://localhost:3000/callback",
                "redirect_uri_provided_explicitly": True,
            }
        )
        await cache.aset(_grant_cache_key(code), grant_data, AUTH_CODE_LIFETIME)
        result = await self.provider.load_authorization_code(info, code)
        self.assertIsNone(result)

    async def test_auth_code_store_load_exchange(self):
        info = _make_client_info()
        await self.provider.register_client(info)

        code = generate_token()
        now = int(time.time())
        grant_data = json.dumps(
            {
                "client_id": "test-client-id",
                "user_id": self.user.id,
                "scopes": ["org:read", "event:read"],
                "expires_at": now + AUTH_CODE_LIFETIME,
                "code_challenge": "challenge",
                "redirect_uri": "http://localhost:3000/callback",
                "redirect_uri_provided_explicitly": True,
                "resource": None,
            }
        )
        await cache.aset(_grant_cache_key(code), grant_data, AUTH_CODE_LIFETIME)

        # Load
        auth_code = await self.provider.load_authorization_code(info, code)
        self.assertIsNotNone(auth_code)
        self.assertEqual(auth_code.code, code)
        self.assertEqual(auth_code.scopes, ["org:read", "event:read"])

        # Exchange
        token = await self.provider.exchange_authorization_code(info, auth_code)
        self.assertIsNotNone(token.access_token)
        self.assertIsNotNone(token.refresh_token)
        self.assertEqual(token.token_type, "Bearer")
        self.assertEqual(token.expires_in, ACCESS_TOKEN_LIFETIME)
        self.assertEqual(token.scope, "org:read event:read")

        # Grant should be consumed
        self.assertIsNone(await cache.aget(_grant_cache_key(code)))

        # Access token should be in cache
        cached = await cache.aget(_access_cache_key(token.access_token))
        self.assertIsNotNone(cached)
        parsed = json.loads(cached)
        self.assertEqual(parsed["user_id"], self.user.id)

        # Refresh token should be in DB
        rt = await OAuthRefreshToken.objects.aget(token=token.refresh_token)
        self.assertEqual(rt.user_id, self.user.id)
        self.assertEqual(rt.access_token_key, token.access_token)


class OAuthProviderRefreshTokenTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()
        self.user = baker.make("users.user", is_active=True)
        self.info = _make_client_info()

    def tearDown(self):
        cache.clear()

    async def test_load_refresh_token_not_found(self):
        result = await self.provider.load_refresh_token(self.info, "nonexistent")
        self.assertIsNone(result)

    async def test_load_refresh_token_revoked(self):
        await self.provider.register_client(self.info)
        await OAuthRefreshToken.objects.acreate(
            token="revoked-token",
            application_id="test-client-id",
            user_id=self.user.id,
            access_token_key="old-access",
            scopes="org:read",
            is_revoked=True,
        )
        result = await self.provider.load_refresh_token(self.info, "revoked-token")
        self.assertIsNone(result)

    async def test_refresh_token_rotation(self):
        await self.provider.register_client(self.info)

        # Set up initial tokens
        old_access = generate_token()
        access_data = json.dumps(
            {
                "user_id": self.user.id,
                "client_id": "test-client-id",
                "scopes": ["org:read"],
                "expires_at": int(time.time()) + ACCESS_TOKEN_LIFETIME,
                "resource": None,
            }
        )
        await cache.aset(
            _access_cache_key(old_access), access_data, ACCESS_TOKEN_LIFETIME
        )

        old_rt = await OAuthRefreshToken.objects.acreate(
            application_id="test-client-id",
            user_id=self.user.id,
            access_token_key=old_access,
            scopes="org:read",
            expires_at=int(time.time()) + 86400,
        )

        refresh = RefreshToken(
            token=old_rt.token,
            client_id="test-client-id",
            scopes=["org:read"],
            expires_at=old_rt.expires_at,
        )

        new_token = await self.provider.exchange_refresh_token(self.info, refresh, [])

        # Old refresh token should be revoked
        await old_rt.arefresh_from_db()
        self.assertTrue(old_rt.is_revoked)

        # Old access token should be removed from cache
        self.assertIsNone(await cache.aget(_access_cache_key(old_access)))

        # New tokens should exist
        self.assertIsNotNone(new_token.access_token)
        self.assertIsNotNone(new_token.refresh_token)
        self.assertNotEqual(new_token.access_token, old_access)
        self.assertNotEqual(new_token.refresh_token, old_rt.token)

        # New access token should be in cache
        self.assertIsNotNone(
            await cache.aget(_access_cache_key(new_token.access_token))
        )

        # New refresh token should be in DB
        new_rt = await OAuthRefreshToken.objects.aget(token=new_token.refresh_token)
        self.assertFalse(new_rt.is_revoked)
        self.assertEqual(new_rt.user_id, self.user.id)


class OAuthProviderLoadAccessTokenTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()
        self.user = baker.make("users.user", is_active=True)

    def tearDown(self):
        cache.clear()

    async def test_load_cached_oauth_token(self):
        token_str = generate_token()
        access_data = json.dumps(
            {
                "user_id": self.user.id,
                "client_id": "test-client",
                "scopes": ["org:read"],
                "expires_at": int(time.time()) + 3600,
                "resource": None,
            }
        )
        await cache.aset(_access_cache_key(token_str), access_data, 3600)

        result = await self.provider.load_access_token(token_str)
        self.assertIsNotNone(result)
        self.assertEqual(result.token, token_str)
        self.assertEqual(result.client_id, str(self.user.id))
        self.assertEqual(result.scopes, ["org:read"])

    async def test_load_expired_cached_token(self):
        token_str = generate_token()
        access_data = json.dumps(
            {
                "user_id": 1,
                "client_id": "test-client",
                "scopes": ["org:read"],
                "expires_at": int(time.time()) - 100,
                "resource": None,
            }
        )
        await cache.aset(_access_cache_key(token_str), access_data, 3600)

        result = await self.provider.load_access_token(token_str)
        self.assertIsNone(result)
        self.assertIsNone(await cache.aget(_access_cache_key(token_str)))

    async def test_load_missing_token(self):
        result = await self.provider.load_access_token("nonexistent")
        self.assertIsNone(result)

    async def test_load_api_token_fallback(self):
        api_token = await sync_to_async(APIToken.objects.create)(
            user=self.user,
        )
        scopes = await sync_to_async(api_token.get_scopes)()

        result = await self.provider.load_access_token(api_token.token)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, AccessToken)
        self.assertEqual(result.token, api_token.token)
        self.assertEqual(result.client_id, str(self.user.id))
        self.assertEqual(result.scopes, scopes)

    async def test_load_api_token_inactive_user_returns_none(self):
        inactive_user = await sync_to_async(baker.make)("users.user", is_active=False)
        api_token = await sync_to_async(APIToken.objects.create)(
            user=inactive_user,
        )

        result = await self.provider.load_access_token(api_token.token)
        self.assertIsNone(result)


class OAuthProviderRevokeTokenTest(TestCase):
    def setUp(self):
        self.provider = GlitchTipOAuthProvider()
        self.user = baker.make("users.user", is_active=True)

    def tearDown(self):
        cache.clear()

    async def test_revoke_access_token(self):
        app = await OAuthApplication.objects.acreate(
            client_id="test-client",
            client_secret="secret",
            client_id_issued_at=int(time.time()),
            client_info={},
        )
        access_str = generate_token()
        await cache.aset(_access_cache_key(access_str), "data", 3600)

        rt = await OAuthRefreshToken.objects.acreate(
            application=app,
            user=self.user,
            access_token_key=access_str,
            scopes="org:read",
        )

        token = AccessToken(
            token=access_str,
            client_id=str(self.user.id),
            scopes=["org:read"],
        )
        await self.provider.revoke_token(token)

        self.assertIsNone(await cache.aget(_access_cache_key(access_str)))
        await rt.arefresh_from_db()
        self.assertTrue(rt.is_revoked)

    async def test_revoke_refresh_token(self):
        app = await OAuthApplication.objects.acreate(
            client_id="test-client",
            client_secret="secret",
            client_id_issued_at=int(time.time()),
            client_info={},
        )
        access_str = generate_token()
        await cache.aset(_access_cache_key(access_str), "data", 3600)

        rt = await OAuthRefreshToken.objects.acreate(
            application=app,
            user=self.user,
            access_token_key=access_str,
            scopes="org:read",
        )

        token = RefreshToken(
            token=rt.token,
            client_id="test-client",
            scopes=["org:read"],
        )
        await self.provider.revoke_token(token)

        await rt.arefresh_from_db()
        self.assertTrue(rt.is_revoked)
        self.assertIsNone(await cache.aget(_access_cache_key(access_str)))

    async def test_revoke_nonexistent_refresh_token(self):
        """Revoking a nonexistent token should be a no-op."""
        token = RefreshToken(
            token="nonexistent",
            client_id="test-client",
            scopes=["org:read"],
        )
        await self.provider.revoke_token(token)
