"""HTTP-level integration tests for the MCP server.

Tests Bearer token auth, transport security (Host header), and the
full JSON-RPC request/response cycle over Streamable HTTP.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
from django.core.cache import cache
from django.test import TestCase
from model_bakery import baker

from apps.mcp.server import mcp
from apps.oauth.provider import _access_cache_key

INITIALIZE_PARAMS = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1.0.0"},
}

ACCEPT_HEADERS = "application/json, text/event-stream"


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _parse_sse_response(text: str) -> dict | None:
    """Extract the JSON-RPC response from an SSE event stream."""
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            if "result" in payload or "error" in payload:
                return payload
    return None


@asynccontextmanager
async def _run_lifespan(app):
    """Send ASGI lifespan events to start/stop the session manager."""
    startup_complete = asyncio.Event()
    shutdown_trigger = asyncio.Event()
    shutdown_complete = asyncio.Event()

    async def receive():
        if not startup_complete.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_trigger.wait()
        return {"type": "lifespan.shutdown"}

    async def send(msg):
        if msg["type"] in ("lifespan.startup.complete", "lifespan.startup.failed"):
            startup_complete.set()
        elif msg["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )
    await startup_complete.wait()
    try:
        yield
    finally:
        shutdown_trigger.set()
        await shutdown_complete.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class MCPHttpAuthTest(TestCase):
    """Tests that only exercise the auth middleware (no lifespan needed)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Reset session manager so we get a fresh app
        mcp._session_manager = None
        cls.app = mcp.streamable_http_app()

    async def _post(self, body, token=None, host="localhost:8000"):
        headers = {
            "Content-Type": "application/json",
            "Accept": ACCEPT_HEADERS,
            "Host": host,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            return await client.post("/mcp", json=body, headers=headers)

    async def test_no_auth_returns_401(self):
        """Requests without a Bearer token should be rejected."""
        resp = await self._post(_jsonrpc("initialize", INITIALIZE_PARAMS))
        self.assertEqual(resp.status_code, 401)

    async def test_invalid_token_returns_401(self):
        resp = await self._post(
            _jsonrpc("initialize", INITIALIZE_PARAMS),
            token="bogus-token-value",
        )
        self.assertEqual(resp.status_code, 401)


def _store_oauth_token(user, scopes=None):
    """Store an OAuth access token in Valkey and return the token string."""
    from apps.api_tokens.models import generate_token

    if scopes is None:
        scopes = ["org:read", "event:read", "project:read", "team:read", "member:read"]
    token_str = generate_token()
    access_data = json.dumps(
        {
            "user_id": user.id,
            "client_id": "test-client",
            "scopes": scopes,
            "expires_at": int(time.time()) + 3600,
            "resource": None,
        }
    )
    cache.set(_access_cache_key(token_str), access_data, 3600)
    return token_str


class MCPHttpIntegrationTest(TestCase):
    """Full-stack tests that require the session manager (lifespan)."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("users.user", is_active=True)
        self.token = _store_oauth_token(self.user)

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def _fresh_app(self):
        """Create a fresh ASGI app with a new session manager."""
        mcp._session_manager = None
        return mcp.streamable_http_app()

    async def test_initialize_with_valid_token(self):
        app = self._fresh_app()
        async with _run_lifespan(app):
            headers = {
                "Content-Type": "application/json",
                "Accept": ACCEPT_HEADERS,
                "Authorization": f"Bearer {self.token}",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    "/mcp",
                    json=_jsonrpc("initialize", INITIALIZE_PARAMS),
                    headers=headers,
                )
            self.assertEqual(resp.status_code, 200)
            payload = _parse_sse_response(resp.text)
            if payload is None:
                payload = resp.json()
            self.assertIn("result", payload)
            self.assertIn("serverInfo", payload["result"])

    async def test_tools_call_with_valid_token(self):
        app = self._fresh_app()
        async with _run_lifespan(app):
            headers = {
                "Content-Type": "application/json",
                "Accept": ACCEPT_HEADERS,
                "Authorization": f"Bearer {self.token}",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    "/mcp",
                    json=_jsonrpc(
                        "tools/call",
                        {"name": "list_organizations", "arguments": {}},
                    ),
                    headers=headers,
                )
            self.assertEqual(resp.status_code, 200)
            payload = _parse_sse_response(resp.text)
            if payload is None:
                payload = resp.json()
            self.assertIn("result", payload)
            content = payload["result"]["content"]
            self.assertTrue(len(content) > 0)
            orgs = json.loads(content[0]["text"])
            self.assertIsInstance(orgs, list)

    async def test_non_localhost_host_header(self):
        """Non-localhost Host header should NOT trigger 421.

        Reproduces the staging bug where DNS rebinding protection
        rejected requests with Host: staging.glitchtip.com.
        """
        app = self._fresh_app()
        async with _run_lifespan(app):
            headers = {
                "Content-Type": "application/json",
                "Accept": ACCEPT_HEADERS,
                "Host": "staging.glitchtip.com",
                "Authorization": f"Bearer {self.token}",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    "/mcp",
                    json=_jsonrpc("initialize", INITIALIZE_PARAMS),
                    headers=headers,
                )
            self.assertNotEqual(resp.status_code, 421)
            self.assertEqual(resp.status_code, 200)
