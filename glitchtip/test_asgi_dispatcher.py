import asyncio

from django.test import SimpleTestCase

from glitchtip.asgi import MCPDjangoDispatcher


class MockASGIApp:
    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope


class LifespanApp:
    """Mock ASGI app that handles lifespan protocol."""

    def __init__(self):
        self.started = False
        self.stopped = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self.started = True
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    self.stopped = True
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        self.scope = scope


class DjangoLikeApp:
    """Mock that raises ValueError on non-http scope (like Django's ASGIHandler)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise ValueError("Django can only handle ASGI/HTTP connections")


class MCPDjangoDispatcherTestCase(SimpleTestCase):
    async def test_mcp_routing(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {"type": "http", "path": "/mcp", "method": "POST"}

        await dispatcher(scope, lambda: None, lambda msg: None)

        self.assertTrue(mock_mcp.called)
        self.assertFalse(mock_django.called)

    async def test_django_routing(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {"type": "http", "path": "/api/0/projects/", "method": "GET"}

        await dispatcher(scope, lambda: None, lambda msg: None)

        self.assertTrue(mock_django.called)
        self.assertFalse(mock_mcp.called)

    async def test_lifespan_forwarded_to_both_apps(self):
        """Both django_app and mcp_app receive lifespan when django_lifespan=True."""
        django_app = LifespanApp()
        mcp_app = LifespanApp()
        dispatcher = MCPDjangoDispatcher(
            django_app, mcp_app, mcp_prefix="/mcp", django_lifespan=True
        )

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        messages = asyncio.Queue()
        await messages.put({"type": "lifespan.startup"})
        await messages.put({"type": "lifespan.shutdown"})

        sent = []

        async def send(msg):
            sent.append(msg)

        await dispatcher(scope, messages.get, send)

        self.assertTrue(django_app.started)
        self.assertTrue(django_app.stopped)
        self.assertTrue(mcp_app.started)
        self.assertTrue(mcp_app.stopped)
        self.assertEqual(sent[0]["type"], "lifespan.startup.complete")
        self.assertEqual(sent[1]["type"], "lifespan.shutdown.complete")

    async def test_lifespan_web_only_mode(self):
        """Lifespan only goes to MCP in web-only mode (django_lifespan=False)."""
        django_app = DjangoLikeApp()
        mcp_app = LifespanApp()
        dispatcher = MCPDjangoDispatcher(django_app, mcp_app, mcp_prefix="/mcp")

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        messages = asyncio.Queue()
        await messages.put({"type": "lifespan.startup"})
        await messages.put({"type": "lifespan.shutdown"})

        sent = []

        async def send(msg):
            sent.append(msg)

        await dispatcher(scope, messages.get, send)

        self.assertTrue(mcp_app.started)
        self.assertTrue(mcp_app.stopped)
        self.assertEqual(sent[0]["type"], "lifespan.startup.complete")
        self.assertEqual(sent[1]["type"], "lifespan.shutdown.complete")
