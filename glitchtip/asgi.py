"""
ASGI config for glitchtip project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/asgi/
"""

import asyncio
import logging
import os
import re

from django.core.asgi import get_asgi_application

from glitchtip.startup import print_startup_banner

logger = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

application = get_asgi_application()

# Print startup banner
print_startup_banner()

# Route ingest paths to a lightweight handler with minimal middleware
from glitchtip.ingest_asgi import IngestDispatcher
from glitchtip.memory_trim import PeriodicMemoryTrim

application = IngestDispatcher(application)

# GlitchTip runs as a single process: the task worker and scheduler live in
# this ASGI application, started from its lifespan. There is no separate
# worker deployment to point at a different entrypoint.
from django_vtasks.asgi import get_worker_application

application = get_worker_application(application)


class MCPDjangoDispatcher:
    """Route MCP and OAuth requests to the MCP Starlette app, everything else to Django.

    The MCP SDK creates OAuth routes (/authorize, /token, /register, /revoke)
    internally at root level. We set issuer_url to include /mcp so the
    well-known metadata advertises them as /mcp/authorize, /mcp/register, etc.
    This avoids conflicts with Django routes (e.g. /register for the SPA).

    Clients discover all OAuth endpoints via /.well-known/oauth-authorization-server
    metadata, so the prefixed paths work transparently.

    Handles ASGI lifespan by forwarding startup/shutdown to both apps so
    the MCP Starlette app can initialise its session-manager task group.
    """

    # OAuth endpoints the MCP SDK creates (under /mcp prefix externally,
    # but the Starlette app expects them at root level internally)
    _OAUTH_SUFFIXES = frozenset({"/authorize", "/token", "/register", "/revoke"})

    def __init__(self, django_app, mcp_app, mcp_prefix="/mcp"):
        self.django_app = django_app
        self.mcp_app = mcp_app
        self.mcp_prefix = mcp_prefix

    # Well-known prefixes that should be routed to the MCP Starlette app
    _WELL_KNOWN_PREFIXES = (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    )

    def _is_mcp_path(self, path: str) -> bool:
        return path.startswith(self.mcp_prefix) or any(
            path.startswith(p) for p in self._WELL_KNOWN_PREFIXES
        )

    def _rewrite_path(self, path: str) -> str:
        """Rewrite external paths to internal paths the MCP SDK expects.

        - /mcp/authorize → /authorize (OAuth endpoints)
        - /.well-known/oauth-authorization-server/mcp → /.well-known/oauth-authorization-server
          (RFC 8414: issuer with path gets suffix on well-known, but SDK registers without it)
        """
        # Strip /mcp prefix from OAuth sub-paths
        if path.startswith(self.mcp_prefix):
            suffix = path[len(self.mcp_prefix) :]
            if suffix in self._OAUTH_SUFFIXES:
                return suffix
        # Strip /mcp suffix from authorization server well-known path only.
        # RFC 8414: the SDK registers oauth-authorization-server WITHOUT the suffix.
        # RFC 9728: the SDK registers oauth-protected-resource WITH the /mcp suffix,
        # so it must NOT be rewritten.
        if path == f"/.well-known/oauth-authorization-server{self.mcp_prefix}":
            return "/.well-known/oauth-authorization-server"
        return path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope["type"] == "http" and self._is_mcp_path(scope["path"]):
            path = self._rewrite_path(scope["path"])
            await self.mcp_app(dict(scope, path=path), receive, send)
        else:
            await self.django_app(scope, receive, send)

    async def _handle_lifespan(self, scope, receive, send):
        """Multiplex ASGI lifespan to both sub-applications.

        The MCP Starlette app needs lifespan to initialise its session-manager
        task group, and django_app is the django-vtasks wrapper, which needs it
        to start the background worker.
        """
        django_queue: asyncio.Queue = asyncio.Queue()
        mcp_queue: asyncio.Queue = asyncio.Queue()
        django_ok = asyncio.Event()
        mcp_ok = asyncio.Event()
        django_done = asyncio.Event()
        mcp_done = asyncio.Event()
        failed = False

        async def django_send(msg):
            nonlocal failed
            if msg["type"] == "lifespan.startup.complete":
                django_ok.set()
            elif msg["type"] == "lifespan.startup.failed":
                failed = True
                django_ok.set()
            elif msg["type"] == "lifespan.shutdown.complete":
                django_done.set()

        async def mcp_send(msg):
            nonlocal failed
            if msg["type"] == "lifespan.startup.complete":
                mcp_ok.set()
            elif msg["type"] == "lifespan.startup.failed":
                failed = True
                mcp_ok.set()
            elif msg["type"] == "lifespan.shutdown.complete":
                mcp_done.set()

        async def run_subapp(app, queue, subapp_send, ok, done, name):
            """Run one sub-app's lifespan, never outliving its own events.

            The waits below are on events only this sub-app sets. A sub-app that
            dies -- raising, or returning without completing the protocol --
            would otherwise leave the handler waiting on a message that can
            never arrive, and a server does not exit a worker until lifespan
            shutdown completes. Settling both events on the way out degrades a
            broken sub-app to a logged failure rather than a wedged worker.
            """
            nonlocal failed
            try:
                await app(scope, queue.get, subapp_send)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ASGI lifespan sub-app %r crashed", name)
            finally:
                if not ok.is_set():
                    # Gone before reporting startup: that is a startup failure.
                    failed = True
                    ok.set()
                done.set()

        async def run_django():
            await run_subapp(
                self.django_app,
                django_queue,
                django_send,
                django_ok,
                django_done,
                "django",
            )

        async def run_mcp():
            await run_subapp(self.mcp_app, mcp_queue, mcp_send, mcp_ok, mcp_done, "mcp")

        tasks = [
            asyncio.create_task(run_django()),
            asyncio.create_task(run_mcp()),
        ]

        try:
            msg = await receive()
            await django_queue.put(msg)
            await mcp_queue.put(msg)
            await django_ok.wait()
            await mcp_ok.wait()

            if failed:
                await send({"type": "lifespan.startup.failed", "message": ""})
                return

            await send({"type": "lifespan.startup.complete"})

            msg = await receive()
            await django_queue.put(msg)
            await mcp_queue.put(msg)
            await django_done.wait()
            await mcp_done.wait()

            await send({"type": "lifespan.shutdown.complete"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import close_old_connections

from glitchtip.async_db import recycle_async_connections


class RecycleConnectionsMiddleware:
    """Recycle stale Django DB connections around a mounted ASGI sub-app.

    The MCP Starlette app is mounted directly in the ASGI callable, so its
    requests never pass through Django's ``BaseHandler`` nor the configured
    ``MIDDLEWARE`` — so neither of the hooks that normally return DB
    connections to the pool each request fires for it:

    * ``close_old_connections`` on the ``request_started`` /
      ``request_finished`` signals ``BaseHandler`` emits (the sync,
      thread-local ``django.db.connections`` store); and
    * ``recycle_async_connections_middleware`` in the configured
      ``MIDDLEWARE``, for the same reason (django-async-backend's per-task
      ``async_connections`` store).

    A connection that dies while held (Postgres failover, pgbouncer restart,
    ``pg_terminate_backend``, a network blip) is therefore never returned to
    the pool: the sub-app pins one dead connection indefinitely and every
    later MCP DB query raises ``OperationalError: the connection is closed``
    until the worker is restarted — while the rest of GlitchTip self-heals,
    because its request path returns connections each request and the pool
    refills with live ones.

    This wrapper restores that per-request recycle for the sub-app. Every MCP
    query — OAuth/token auth and the tool handlers alike — reaches the DB via
    Django's native async ORM (``aget`` / ``async for``), which runs its sync
    work through ``sync_to_async`` on the process-wide thread-sensitive
    executor thread; the connection wrapper therefore lives in the *sync*,
    thread-local store on that one thread. Running ``close_old_connections``
    through ``sync_to_async`` lands it on that same thread, so it recycles the
    exact wrapper those queries use — including the queries the MCP SDK
    dispatches in its stateless child task, which shares the same executor
    thread (no ``ThreadSensitiveContext`` splits the MCP path). MCP does not
    currently touch the ``async_connections`` store, but
    ``recycle_async_connections`` is called too for parity with the normal
    request path. On a healthy connection both calls are no-ops, so
    steady-state cost is negligible.

    Recovery matches the rest of GlitchTip: a request in flight during the
    switchover may still see a transient error, but the sub-app no longer
    stays wedged afterwards.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only HTTP requests touch the ORM; forward lifespan/websocket as-is.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Recycle before the request too, so a connection that died while the
        # wrapper sat idle between requests is returned to the pool up front.
        await _recycle_db_connections()
        try:
            await self.app(scope, receive, send)
        finally:
            # Cancellation safety lives inside _recycle_db_connections:
            # shielding the call from here would run the async half in a
            # child task, whose context changes never reach this one.
            await _recycle_db_connections()


async def _recycle_db_connections():
    # Guard like glitchtip.task_signals._recycle_async_connections: returning a
    # broken connection can itself raise (e.g. putconn under
    # wrap_database_errors during a failover), and an unhandled raise here
    # would 500 the request (pre-call) or mask the response's own exception
    # (post-call). Log and move on; the next query reconnects on demand.
    try:
        # Both halves have to survive a client disconnect mid-response, or a
        # connection leaks into the next request. The async half goes first
        # because it must not be wrapped in a shield of its own — it rewrites
        # a context-variable store, so it has to run in this task's context,
        # and it shields its own closing internally. The sync half runs from
        # a ``finally``, so a cancellation delivered to the first still
        # reaches it.
        try:
            await recycle_async_connections()
        finally:
            await asyncio.shield(sync_to_async(close_old_connections)())
    except Exception:
        logger.exception("Failed to recycle DB connections for MCP request")


if settings.GLITCHTIP_ENABLE_MCP:
    from apps.mcp.server import build_http_app

    _mcp_app = build_http_app()
    application = MCPDjangoDispatcher(
        django_app=application,
        mcp_app=RecycleConnectionsMiddleware(_mcp_app),
    )

# Serve /static/ here, above Django.
#
# ServeStatic is an ASGI app, not middleware: it answers from an index built at
# startup and delegates everything else inward, so a static hit never builds a
# Django request or walks MIDDLEWARE. It negotiates the .br/.gz copies
# collectstatic writes beside each file (see STORAGES["staticfiles"]), which
# granian's --static-path-mount cannot do -- granian has no compression support
# at all -- and it applies cache headers per file rather than one expiry for the
# whole tree.
#
# Configured here in code rather than through servestatic's SERVESTATIC_*
# settings: those are read by its Django middleware, which this deliberately
# does not use. Only the ones the storage backend reads apply.
from servestatic import ServeStaticASGI
from servestatic.responders import StaticFile
from servestatic.utils import decode_path_info

# Frozen at import, which is the only point it is well defined: django.setup()
# has already installed FORCE_SCRIPT_NAME as the script prefix, so this carries
# any BASE_PATH the app is mounted under and matches the path the ASGI scope
# carries. Re-reading settings.STATIC_URL per request would read a value that
# depends on when it was first read.
_STATIC_PREFIX = settings.STATIC_URL

# Everything that is not content-addressed is refetched this often, so an
# upgrade cannot leave a client pinned to a stale copy for longer than this.
_STATIC_MAX_AGE = 3600

# The frontend build emits content-hashed bundles at the root of the collected
# tree (main-<hash>.js, chunk-<hash>.js, and the .map beside each). Their names
# change whenever their contents do, so they are safe to cache forever.
#
# Hash width is not fixed -- the build mixes 8- and 9-character hashes -- so
# this must not assume one width. The lookahead demands an upper-case letter or
# a digit somewhere in the segment: every hash the build emits has one and
# ordinary words do not, so a hand-added `main-override.css` or
# `vendor-analytics.js` keeps the short TTL instead of matching the shape.
# `immutable` is the one header a later deploy cannot take back, so where the
# rule is unsure it has to be wrong in the harmless direction.
_HASHED_BUNDLE = re.compile(
    r"-(?=[A-Za-z0-9_-]*[A-Z0-9])[A-Za-z0-9_-]{8,10}\.(?:js|css)(?:\.map)?$"
)


def _is_hashed_asset(path: str, url: str) -> bool:
    """True only for the build's own hashed bundles at the tree root.

    Everything in a subdirectory -- assets/ (i18n, fonts, images, none of them
    content-hashed), admin/, allauth's -- keeps the short TTL, so a release
    cannot leave a client pinned to a stale copy.
    """
    rel = url.removeprefix(_STATIC_PREFIX)
    if "/" in rel:
        return False
    return bool(_HASHED_BUNDLE.search(rel))


def _add_security_headers(headers, path: str, url: str) -> None:
    """Set the security headers MIDDLEWARE would have set.

    Nothing below this layer runs for a file ServeStatic answers, so none of
    SecurityMiddleware's or XFrameOptionsMiddleware's headers reach a static
    response. ``nosniff`` belongs on the whole tree. The framing headers matter
    because collectstatic copies the single-page app's index.html in with
    everything else, and that copy boots the real, session-authenticated app
    from the app's own origin -- without them it is a frameable, CSP-free second
    front door to the same session.
    """
    headers["X-Content-Type-Options"] = "nosniff"
    if headers.get("Content-Type", "").startswith("text/html"):
        headers["X-Frame-Options"] = "DENY"
        headers["Content-Security-Policy"] = "frame-ancestors 'none'"


class PathSendServeStatic(ServeStaticASGI):
    """ServeStatic that hands the file to the server instead of reading it.

    Reading a static file from Python costs a thread, because asyncio has no
    file I/O to offer: epoll refuses regular files, so every "async file" in the
    ecosystem is a thread pool underneath. servestatic builds one
    ``ThreadPoolExecutor`` per response and reads in 8 KiB blocks, and the bytes
    cross into Python only to be handed straight back for the socket write.

    The ``http.response.pathsend`` ASGI extension removes that round trip: we
    send the headers this layer computed, then name the file, and the server
    transfers it from its own runtime. Python never opens it -- a static hit
    makes no filesystem call at all.

    Servers that do not implement the extension leave it out of
    ``scope["extensions"]`` and get the streaming path unchanged, so this stays
    a pure optimisation rather than a requirement. GlitchTip ships granian,
    which implements it.
    """

    async def __call__(self, scope, receive, send) -> None:
        target = self._pathsend_target(scope)
        if target is None:
            await super().__call__(scope, receive, send)
            return
        path, headers = target
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (key.lower().encode("latin-1"), value.encode("latin-1"))
                    for key, value in headers
                    if value is not None
                ],
            }
        )
        await send({"type": "http.response.pathsend", "path": path})

    def _pathsend_target(self, scope):
        """The file to hand over, or None to fall back to streaming."""
        if scope["type"] != "http" or scope.get("method") != "GET":
            return None
        if "http.response.pathsend" not in (scope.get("extensions") or {}):
            return None
        if self.autorefresh:
            # Development resolves per request against the finders rather than
            # a startup index, and has no precompressed copies to choose
            # between; it gains nothing here.
            return None
        static_file = self.files.get(decode_path_info(scope["path"]))
        if not isinstance(static_file, StaticFile):
            return None
        request_headers = {
            "HTTP_" + key.decode("latin-1").upper().replace("-", "_"): value.decode(
                "latin-1"
            )
            for key, value in scope["headers"]
        }
        # Conditional and range requests have responses of their own, and the
        # library already builds those correctly.
        if "HTTP_RANGE" in request_headers or static_file.is_not_modified(
            request_headers
        ):
            return None
        return static_file.get_path_and_headers(request_headers)


def serve_static(app):
    """Wrap ``app`` so requests under STATIC_URL are answered before Django."""
    if not settings.DEBUG:
        return PathSendServeStatic(
            app,
            root=settings.STATIC_ROOT,
            prefix=_STATIC_PREFIX,
            max_age=_STATIC_MAX_AGE,
            immutable_file_test=_is_hashed_asset,
            add_headers_function=_add_security_headers,
        )

    # Development has no collected tree: `docker compose up` runs the repo as it
    # stands and never calls collectstatic, so indexing STATIC_ROOT would find
    # nothing and every asset the admin, allauth and the SPA ask for would 404.
    # Register the directories the staticfiles finders search instead and
    # resolve against them per request -- which is what WhiteNoise did under
    # DEBUG, and what runserver does.
    #
    # Registering directories rather than calling finders.find() keeps
    # ServeStatic's own containment check, which compares resolved paths and so
    # refuses a symlink inside a static directory that points out of it.
    #
    # Nothing is cached, including the hashed bundles: a dev build can be
    # configured not to hash at all, and an asset rebuilt under its own name has
    # to show up on the next reload.
    static_app = PathSendServeStatic(
        app,
        autorefresh=True,
        max_age=0,
        add_headers_function=_add_security_headers,
    )
    for finder in finders.get_finders():
        # Both finders Django enables by default expose their directories this
        # way. A third-party finder that does not would simply not be served in
        # development.
        for storage in getattr(finder, "storages", {}).values():
            prefix = _STATIC_PREFIX + (getattr(storage, "prefix", None) or "")
            static_app.add_files(storage.location, prefix=prefix)
    return static_app


application = serve_static(application)

# Wrap application with granian proxy headers support
# This allows granian to properly handle X-Forwarded-For and X-Forwarded-Proto headers
# when running behind a reverse proxy (nginx, traefik, k8s ingress, etc.)
try:
    from granian.utils.proxies import wrap_asgi_with_proxy_headers

    # TRUSTED_PROXIES can be:
    # - "*" to trust all proxies (default, safe when granian isn't directly exposed)
    # - A comma-separated list of IPs/CIDR ranges (e.g., "10.0.0.0/8,172.16.0.0/12")
    # - For k8s: "*" is recommended since pod IPs are dynamic and pods aren't exposed
    trusted_proxies = os.environ.get("TRUSTED_PROXIES", "*")
    if "," in trusted_proxies:
        trusted_proxies = [ip.strip() for ip in trusted_proxies.split(",")]

    application = wrap_asgi_with_proxy_headers(
        application, trusted_hosts=trusted_proxies
    )
except ImportError:
    # Graceful fallback if granian can't be imported (custom installations
    # serving this app under another ASGI server)
    pass

# Outermost so lifespan startup reaches it at all (the vtasks wrapper consumes
# lifespan without forwarding it inward): a pod receiving no HTTP traffic still
# starts its trim timer. Periodically
# returns freed memory to the OS in every ASGI process — the scheduled
# maintenance task only trims the one pod that runs it.
application = PeriodicMemoryTrim(application)
