import asyncio
from unittest import mock

from asgiref.sync import sync_to_async
from django.test import SimpleTestCase, TransactionTestCase

from glitchtip.asgi import (
    MCPDjangoDispatcher,
    RecycleConnectionsMiddleware,
    _recycle_db_connections,
)


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


class CrashingLifespanApp:
    """Mock whose lifespan task raises instead of completing the protocol.

    Models a real failure: the embedded vtasks worker raises on startup against
    an unsupported task backend, so its lifespan task dies without ever sending
    startup.complete or shutdown.complete.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            raise RuntimeError("lifespan task died")


class SilentLifespanApp:
    """Mock that reports startup, then returns without completing shutdown."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            await receive()
            return


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

    async def test_lifespan_shutdown_completes_when_subapp_crashes(self):
        """A crashed sub-app must not wedge lifespan shutdown.

        The handler waits on events only each sub-app sets, so a sub-app whose
        lifespan task raises can never set them. Servers do not exit a worker
        until lifespan shutdown completes, and granian starts the replacement
        worker before the old one goes away -- so a wedge here parks two workers
        at once and the cgroup OOM killer becomes what stops the old one.
        """
        django_app = CrashingLifespanApp()
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

        # wait_for so a regression fails here instead of hanging the suite.
        with self.assertLogs("glitchtip.asgi", level="ERROR"):
            await asyncio.wait_for(dispatcher(scope, messages.get, send), timeout=10)

        # The crash is reported as a startup failure rather than a hang.
        self.assertEqual(sent[0]["type"], "lifespan.startup.failed")

    async def test_lifespan_shutdown_completes_when_subapp_exits_early(self):
        """A sub-app that returns without completing shutdown must not wedge it."""
        django_app = SilentLifespanApp()
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

        await asyncio.wait_for(dispatcher(scope, messages.get, send), timeout=10)

        # Startup was reported, so shutdown must still complete rather than hang.
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


class RecycleConnectionsMiddlewareTestCase(SimpleTestCase):
    async def test_http_recycles_before_and_after_request(self):
        """Stale DB connections are recycled around each HTTP request.

        The MCP sub-app never passes through Django's request signals or
        MIDDLEWARE, so this wrapper must recycle the connection itself both
        before (replace a connection that died while idle) and after
        (return it, mirroring request_finished) the app runs.
        """
        order = []
        inner = MockASGIApp()

        async def track_app(scope, receive, send):
            order.append("app")
            await inner(scope, receive, send)

        async def track_recycle():
            order.append("recycle")

        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", side_effect=track_recycle
        ):
            middleware = RecycleConnectionsMiddleware(track_app)
            scope = {"type": "http", "path": "/mcp", "method": "POST"}
            await middleware(scope, lambda: None, lambda msg: None)

        self.assertTrue(inner.called)
        self.assertEqual(order, ["recycle", "app", "recycle"])

    async def test_non_http_scope_skips_recycle(self):
        """Lifespan/websocket scopes never touch the ORM and pass through."""
        inner = MockASGIApp()
        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", new_callable=mock.AsyncMock
        ) as recycle:
            middleware = RecycleConnectionsMiddleware(inner)
            scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
            await middleware(scope, lambda: None, lambda msg: None)

        self.assertTrue(inner.called)
        recycle.assert_not_awaited()

    async def test_recycle_runs_after_request_raises(self):
        """Cleanup still runs (in finally) when the wrapped app errors."""

        async def failing_app(scope, receive, send):
            raise RuntimeError("boom")

        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", new_callable=mock.AsyncMock
        ) as recycle:
            middleware = RecycleConnectionsMiddleware(failing_app)
            scope = {"type": "http", "path": "/mcp", "method": "POST"}
            with self.assertRaises(RuntimeError):
                await middleware(scope, lambda: None, lambda msg: None)

        # Once before the request, once in the finally.
        self.assertEqual(recycle.await_count, 2)


def _kill_thread_local_connection():
    """Simulate a failover: drop the backing socket but leave the wrapper
    thinking it is still connected (as it would be after the server closed
    the connection out from under us). Run this via ``sync_to_async`` so it
    hits the same thread-sensitive executor thread the async ORM uses."""
    from django.db import connection

    if connection.connection is not None:
        connection.connection.close()
    connection.errors_occurred = True


class RecycleDBConnectionsTestCase(TransactionTestCase):
    async def test_recycle_is_a_noop_on_healthy_connection(self):
        """Recycling must not break a live connection (the steady-state path)."""
        from apps.projects.models import Project

        await Project.objects.acount()
        await _recycle_db_connections()
        await Project.objects.acount()

    async def test_middleware_recovers_dead_connection(self):
        """The #489 scenario: a connection that dies while held is recovered.

        Without recycling, the wrapper keeps its dead handle and the next
        query raises "the connection is closed". Driving a query through the
        middleware after the connection is killed must succeed, because the
        pre-request recycle returns the dead connection and the query
        reconnects.
        """
        from apps.projects.models import Project

        # Warm the sync connection on the shared thread-sensitive thread.
        await Project.objects.acount()
        await sync_to_async(_kill_thread_local_connection)()

        result = {}

        async def app(scope, receive, send):
            result["count"] = await Project.objects.acount()

        middleware = RecycleConnectionsMiddleware(app)
        await middleware(
            {"type": "http", "path": "/mcp", "method": "POST"},
            lambda: None,
            lambda msg: None,
        )

        # Query after the recycle succeeded; a wedged connection would have
        # raised OperationalError instead of reaching this assertion.
        self.assertIn("count", result)
