"""Recycling for django-async-backend's ``async_connections`` store.

django-async-backend stamps a connection wrapper with the asyncio task
that first used it and refuses every other task ("An async connection can
only be used by the task that first used it"). Closing the connection
does not clear that stamp, so a wrapper that outlives the task which
claimed it stays permanently unusable — the next task to reach for the
same alias gets a ``RuntimeError`` instead of a cursor.

A wrapper outlives its task whenever asgiref copies context back to a
caller. ``async_connections`` is an ``asgiref.local.Local``: in an async
thread it is backed by a context variable, so a task that opens a
connection normally keeps it to itself. But ``sync_to_async`` and
``async_to_sync`` deliberately restore the context they ran under back
into the caller, and Django's request path crosses that boundary whenever
sync and async middleware interleave. The wrapper a view claimed
therefore lands in the caller's context and is inherited by whatever runs
next — the following request, or the rest of a test.

So recycling has to do both halves: close the connection *and* put a
fresh, unclaimed wrapper in the store. Replacing the entry is what
``async_new_connection()`` does to hand its block a private connection;
here it hands the next task one. Creating a wrapper opens nothing, so the
replacement costs an object and the connection itself is only opened if
something queries.
"""

import asyncio
import logging

from django_async_backend.db import async_connections

logger = logging.getLogger(__name__)


async def recycle_async_connections(**_kwargs):
    """Close this context's async connections and unclaim their aliases.

    Safe to call from any task and at any point in a request or task
    lifecycle: aliases this context never touched are left alone, and a
    wrapper with no open connection closes as a no-op.

    Connected to ``request_started`` / ``request_finished`` in place of
    django-async-backend's own ``close_old_async_connections`` (which
    closes but does not unclaim) — see
    ``ASYNC_BACKEND_DISABLE_REQUEST_SIGNALS`` in settings.

    Await this directly. Wrapping the call in ``asyncio.shield()`` or
    ``create_task()`` would run it in a child context, and the store is a
    context variable: the fresh wrappers would land somewhere the caller
    never looks. The close is shielded from here instead, so a cancelled
    request or task still returns its connection.
    """
    for conn in async_connections.all(initialized_only=True):
        alias = conn.alias
        # Unclaim first. If the close below is interrupted, an alias left
        # claimed by a task that is already gone is the worse outcome: the
        # pool reaps a leaked socket, nothing reclaims a stale stamp.
        async_connections[alias] = async_connections.create_connection(alias)
        try:
            await asyncio.shield(conn.close())
        except Exception:
            # Returning a broken connection can itself raise (e.g. putconn
            # during a failover), and an unhandled raise here would 500 the
            # request. The pool reclaims the socket; carry on.
            logger.exception("Failed to close async DB connection %r", alias)
