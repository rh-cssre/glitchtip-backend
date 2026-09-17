"""Recycling for django-async-backend's ``async_connections`` store.

django-async-backend stamps a connection wrapper with the asyncio task
that first used it and refuses every other task ("An async connection can
only be used by the task that first used it"). Closing the connection
does not clear that stamp, so a wrapper that outlives the task which
claimed it stays permanently unusable — the next task to reach for the
same alias gets a ``RuntimeError`` instead of a cursor.

A wrapper outlives its task whenever a context is copied back to a
caller. ``async_connections`` is an ``asgiref.local.Local``: in an async
thread it is backed by a context variable, so a task that opens a
connection normally keeps it to itself. But ``sync_to_async`` and
``async_to_sync`` deliberately restore the context they ran under back
into the caller, and so does ``Signal.asend``, which dispatches async
receivers in child tasks and calls asgiref's ``_restore_context`` on the
way out. Django's request path crosses those boundaries whenever sync and
async middleware interleave, so the wrapper a view claimed lands in the
caller's context and is inherited by whatever runs next — the following
request, or the rest of a test.

So recycling has to do both halves: close the connection *and* put a
fresh, unclaimed wrapper in the store. Replacing the entry is what
``async_new_connection()`` does to hand its block a private connection;
here it hands the next task one. Creating a wrapper opens nothing, so the
replacement costs an object and the connection itself is only opened if
something queries.
"""

import asyncio
import logging

from django.core.checks import Error, register
from django.utils.decorators import async_only_middleware
from django_async_backend.db import async_connections

logger = logging.getLogger(__name__)

MIDDLEWARE_PATH = "glitchtip.async_db.recycle_async_connections_middleware"


def _holds_anything(conn) -> bool:
    """Whether this wrapper has a stamp to clear or a socket to return.

    ``_task`` is private, but it *is* the stamp this module exists to
    clear and django-async-backend exposes no public reader for it. A
    wrapper with neither is one the store handed out and nobody used;
    recycling it would allocate a replacement per request for an object
    that was never touched.
    """
    return conn.connection is not None or conn._task is not None


async def recycle_async_connections():
    """Close this context's async connections and unclaim their aliases.

    Meant for a request or task boundary. Aliases this context never
    touched are left alone, and an alias with a transaction open is
    skipped rather than torn out from under it — mirroring Django's own
    ``close_if_unusable_or_obsolete``.

    Await this directly. Wrapping the call in ``asyncio.shield()`` or
    ``create_task()`` puts it in a bare asyncio child task, whose context
    changes are never copied back (unlike ``Signal.asend`` or
    ``sync_to_async``, which restore theirs) — the fresh wrappers would
    land where the caller never looks. Every store write below therefore
    happens before the first await, so a cancellation can interrupt the
    closing but never leave an alias claimed, and only the closes are
    shielded.
    """
    recycled = []
    for conn in async_connections.all(initialized_only=True):
        if not _holds_anything(conn):
            continue
        if conn.in_atomic_block:
            logger.warning(
                "Not recycling async DB connection %r: a transaction is open",
                conn.alias,
            )
            continue
        async_connections[conn.alias] = async_connections.create_connection(conn.alias)
        recycled.append(conn)

    if not recycled:
        return

    # One shield over every alias: a per-alias await would raise at the
    # first suspension point and leave the rest of the list unclosed.
    # ``return_exceptions`` both keeps one alias that can't be returned
    # (putconn during a failover) from stranding the others, and makes
    # sure the shielded task's result is always retrieved — a bare raise
    # inside a shield the caller stopped awaiting surfaces as an
    # unattributed "exception was never retrieved" asyncio log.
    results = await asyncio.shield(
        asyncio.gather(*(conn.close() for conn in recycled), return_exceptions=True)
    )
    for conn, result in zip(recycled, results):
        if isinstance(result, BaseException):
            logger.error(
                "Failed to close async DB connection %r",
                conn.alias,
                exc_info=result,
            )


@async_only_middleware
def recycle_async_connections_middleware(get_response):
    """Recycle ``async_connections`` around every request.

    A middleware rather than a ``request_started`` / ``request_finished``
    receiver, which is what django-async-backend 6.1 connects for itself:
    an async receiver forces ``Signal.send`` through ``async_to_sync``,
    which blocks the one process-wide thread-sensitive executor thread on
    a round trip to the event loop for as long as the recycle takes. This
    runs the same work inline in the request's own task, which is also
    where the wrappers to unclaim are. It sits at the head of the chain so
    connections go back before anything else finalises the response.

    Recycling on the way in as well, so a connection that died while the
    wrapper sat idle between requests is returned up front.
    """

    async def middleware(request):
        await recycle_async_connections()
        try:
            return await get_response(request)
        finally:
            await recycle_async_connections()

    return middleware


@register()
def check_async_connection_recycling(**kwargs):
    """Fail loudly if nothing recycles ``async_connections``.

    Django passes ``app_configs`` and ``databases`` by keyword; this check
    reads settings rather than models, so it takes neither.

    django-async-backend's own request receivers are switched off
    (``ASYNC_BACKEND_DISABLE_REQUEST_SIGNALS``) because they close without
    unclaiming. If the middleware that replaces them is dropped from
    either handler's chain, nothing clears the stamp and the second task
    to use an alias raises — a 500 on every ingest and API path that
    reads through ``apps.shared.raw_sql``, with nothing to point at.
    """
    from django.conf import settings

    from glitchtip.ingest_asgi import INGEST_MIDDLEWARE

    if not getattr(settings, "ASYNC_BACKEND_DISABLE_REQUEST_SIGNALS", False):
        return []
    return [
        Error(
            f"{MIDDLEWARE_PATH} is missing from {name}.",
            hint=(
                "An async DB connection stays claimed by the task that first "
                "used it, so without this middleware the next task to use the "
                "alias raises RuntimeError. Add it back, or unset "
                "ASYNC_BACKEND_DISABLE_REQUEST_SIGNALS to fall back on "
                "django-async-backend's own receivers."
            ),
            id="glitchtip.E001",
        )
        for name, chain in (
            ("MIDDLEWARE", settings.MIDDLEWARE),
            ("glitchtip.ingest_asgi.INGEST_MIDDLEWARE", INGEST_MIDDLEWARE),
        )
        if MIDDLEWARE_PATH not in chain
    ]
