"""ASGI handler serving ``POST /api/<project_id>/envelope/`` in Rust.

When ``GLITCHTIP_RUST_INGEST`` is enabled, ``IngestDispatcher`` routes
envelope POSTs here instead of the Django view. The whole request — DSN
auth + throttling, body buffering, decompression/framing, item validation,
PII scrubbing, event-id dedupe and the vtasks enqueue — runs inside
``gt_rust`` on the shared tokio runtime; this module only adapts ASGI
messages to the ``gt_rust.ingest.IngestSession`` calls and reports the
pipeline's anomalies to the error tracker.

The Rust path shares the process's existing I/O handles: the
``gt_rust.django_backend`` connection pool (auth query) and the vcache
Valkey driver (block cache, dedupe, task queue). Nothing per-request is
allocated on the Python heap beyond the ASGI messages themselves.

Envelopes the Rust pipeline cannot fully own — SDK minidump submissions,
which need the ``symbolic`` stack — come back as a fallback and are
replayed through the regular (minimal-middleware) Django ingest handler
before any side effect happened.
"""

import logging
import os
import re

import orjson
import sentry_sdk

logger = logging.getLogger(__name__)

_ENVELOPE_PATH_RE = re.compile(r"^/api/(\d+)/envelope/$")

# Project ids are BIGINT; the Rust IngestSession takes an i64. A larger id
# can't name a real project, so it must not overflow the session argument.
_MAX_PROJECT_ID = 9223372036854775807

_init_pid: int | None = None


def _build_config() -> dict:
    from django.conf import settings
    from django.core.cache import caches
    from django_vtasks.conf import settings as vtasks_settings

    # The Rust path builds cache/dedupe/block keys and the vtasks queue key
    # itself, so it must use the exact prefixes Django's cache and vtasks
    # backends use. Emitting them (rather than relying on the Rust-side
    # defaults) makes a non-default cache KEY_PREFIX/VERSION or
    # VTASKS_VALKEY_PREFIX work instead of silently missing every key — worst
    # case LPUSHing to a queue the worker never drains. ``make_key("")`` is
    # exactly the prefix prepended to a raw cache key.
    cache_key_prefix = caches["default"].make_key("")

    return {
        "max_unzipped": settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE,
        "max_body": settings.DATA_UPLOAD_MAX_MEMORY_SIZE,
        "enable_logs": settings.GLITCHTIP_ENABLE_LOGS,
        "billing_enabled": settings.BILLING_ENABLED,
        "throttle_check_interval": settings.GLITCHTIP_THROTTLE_CHECK_INTERVAL,
        "transaction_retention_days": settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS,
        "transaction_future_skew_secs": int(
            settings.GLITCHTIP_TRANSACTION_FUTURE_SKEW.total_seconds()
        ),
        "default_scrub_config_json": (
            orjson.dumps(settings.GLITCHTIP_PII_SCRUB_DEFAULT).decode()
            if settings.GLITCHTIP_PII_SCRUB_DEFAULT
            else None
        ),
        "cors_allow_all": settings.CORS_ORIGIN_ALLOW_ALL,
        "cors_whitelist": [str(o) for o in settings.CORS_ORIGIN_WHITELIST],
        "cache_key_prefix": cache_key_prefix,
        "vtasks_prefix": vtasks_settings.VTASKS_VALKEY_PREFIX,
    }


def _pg_driver(alias: str):
    """The RustPgDriver the ingest path issues its auth queries on.

    Production: the process-shared driver behind Django's connection, so
    ingest and the ORM share one pool. Under the test runner Django sets
    ``OPTIONS["pool"] = False``, which gives each Django connection a
    private 1-slot driver — sharing THAT would starve the ingest auth
    query behind the test's own pinned session, so connect a small
    dedicated pool to the (test) database instead.
    """
    from django.conf import settings
    from django.db import connections

    wrapper = connections[alias]
    wrapper.ensure_connection()
    if not settings.TESTING:
        return wrapper.connection._driver

    import gt_rust.dbapi

    sd = wrapper.settings_dict
    return gt_rust.dbapi.connect(
        host=sd["HOST"] or "localhost",
        port=int(sd["PORT"] or 5432),
        dbname=sd["NAME"],
        user=sd["USER"],
        password=sd["PASSWORD"] or "",
        pool_size=2,
        application_name="gt-ingest-test",
    )._driver


def test_valkey_url() -> str:
    """A VALKEY_URL pointing at a dedicated database for the test suite, so
    test traffic can never reach a live worker's queue."""
    from urllib.parse import urlsplit, urlunsplit

    from django.conf import settings

    parts = urlsplit(settings.VALKEY_URL)
    return urlunsplit(parts._replace(path="/9"))


def _valkey_driver():
    """The Valkey driver the ingest path issues its commands on.

    Production: the very driver behind Django's vcache cache (``DRIVER_CLASS``
    is gt_rust's class unconditionally, so cache and ingest share one
    connection). Under the test runner the cache is LocMem, so connect a
    dedicated driver to the test database instead.
    """
    from django.conf import settings
    from django.core.cache import caches

    cache_backend = caches["default"]
    if hasattr(cache_backend, "_driver"):
        return cache_backend._driver
    if not settings.TESTING:
        raise RuntimeError(
            "GLITCHTIP_RUST_INGEST requires the vcache Valkey cache backend"
        )
    from gt_rust.valkey import RustValkeyDriver

    return RustValkeyDriver.connect(test_valkey_url())


def _init_sync() -> None:
    """Wire gt_rust.ingest to the shared driver handles (idempotent per
    process; re-runs after fork)."""
    global _init_pid
    if _init_pid == os.getpid():
        return

    import gt_rust.ingest
    from django.conf import settings

    read_pg = _pg_driver("read_only") if "read_only" in settings.DATABASES else None
    gt_rust.ingest.init(
        _pg_driver("default"),
        _valkey_driver(),
        _build_config(),
        read_pg,
    )
    _init_pid = os.getpid()


def _report_anomalies(anomalies: list[dict], path: str) -> None:
    """Mirror the Python view's sentry captures for envelope oddities.

    Anomaly fields are attacker-controlled — they are recorded as context
    data, never interpreted.
    """
    for anomaly in anomalies:
        kind = anomaly.get("kind", "unknown")
        item_type = anomaly.get("item_type")
        with sentry_sdk.new_scope() as scope:
            scope.level = "warning"
            if kind == "unsupported-item-type":
                # Same fingerprint the Python view uses, so a genuinely-new
                # SDK item type keeps surfacing as its own issue.
                scope.fingerprint = [
                    "envelope-unsupported-item-type",
                    item_type or "unknown",
                ]
                scope.set_tag("envelope_item_type", item_type or "unknown")
            scope.set_context("rust ingest anomaly", anomaly)
            sentry_sdk.capture_message(
                f"Envelope ingest anomaly ({kind}) on {path}", "warning"
            )
        logger.warning(
            "Envelope ingest anomaly (%s) for item type %r on %s: %s",
            kind,
            item_type,
            path,
            anomaly.get("message"),
        )


def _cors_headers(scope) -> list:
    """CORS headers for a response the Rust pipeline didn't build (500/503),
    matching gt_ingest's ``apply_cors`` so a browser SDK can read the status
    instead of getting a CORS-blocked opaque error."""
    from django.conf import settings

    origin = None
    for name, value in scope.get("headers") or ():
        if name == b"origin":
            origin = value.decode("latin1")
            break
    if origin is None:
        return []
    if settings.CORS_ORIGIN_ALLOW_ALL:
        return [("Access-Control-Allow-Origin", "*")]
    if origin in {str(o) for o in settings.CORS_ORIGIN_WHITELIST}:
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]
    return []


async def _send_response(send, status: int, headers: list, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (name.encode("latin1"), value.encode("latin1"))
                for name, value in headers
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RustEnvelopeHandler:
    """Handles matching requests fully in Rust; everything else (and Rust
    fallbacks) goes to ``fallback_app`` — the minimal-middleware Django
    ingest handler."""

    def __init__(self, fallback_app):
        self._fallback_app = fallback_app

    @staticmethod
    def match(scope) -> int | None:
        """The project id when this handler owns the request, else None."""
        if scope.get("method") != "POST":
            return None
        match = _ENVELOPE_PATH_RE.match(scope.get("path", ""))
        if not match:
            return None
        project_id = int(match.group(1))
        # Out-of-range: let the Django ingest path answer (403/404) rather
        # than overflow the i64 session arg into a 500 + error capture.
        if project_id > _MAX_PROJECT_ID:
            return None
        return project_id

    async def __call__(self, scope, receive, send, project_id: int) -> None:
        try:
            await self._handle(scope, receive, send, project_id)
        except Exception as e:
            # Same contract as Django's handler: an unexpected failure is a
            # 500, reported, never a dropped connection.
            sentry_sdk.capture_exception(e)
            logger.exception("Rust ingest path failed on %s", scope.get("path", ""))
            try:
                await _send_response(
                    send,
                    500,
                    [("Content-Type", "application/json"), *_cors_headers(scope)],
                    b'{"detail": "Internal server error"}',
                )
            except Exception:
                # Response already started — nothing more to send.
                pass

    async def _handle(self, scope, receive, send, project_id: int) -> None:
        from django.conf import settings
        from gt_rust.ingest import IngestSession

        if settings.MAINTENANCE_EVENT_FREEZE:
            await _send_response(
                send,
                503,
                [("Content-Type", "application/json"), *_cors_headers(scope)],
                b'{"detail": "Events are not currently being accepted due to maintenance."}',
            )
            return

        if _init_pid != os.getpid():
            from asgiref.sync import sync_to_async

            await sync_to_async(_init_sync)()

        client = scope.get("client")
        session = IngestSession(
            project_id,
            scope.get("query_string", b"").decode("latin1"),
            list(scope.get("headers") or ()),
            client[0] if client else None,
        )

        rejection = await session.authenticate()
        if rejection is not None:
            _, status, body, headers = rejection
            await _send_response(send, status, headers, body)
            return

        # Stream the body into Rust; Python holds only one chunk at a time.
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk and not session.feed(chunk):
                break  # over the body cap; finish() responds 413
            if not message.get("more_body", False):
                break

        result = await session.finish()
        if result[0] == "fallback":
            await self._replay(scope, result[1], receive, send)
            return

        _, status, body, headers, anomalies = result
        await _send_response(send, status, headers, body)
        if anomalies:
            _report_anomalies(anomalies, scope.get("path", ""))

    async def _replay(self, scope, body: bytes, receive, send) -> None:
        """Re-dispatch the buffered request through the Django ingest app."""
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # The real body is consumed; behave like a quiet connection so
            # Django's disconnect listener idles until the response is done.
            return await receive()

        await self._fallback_app(scope, replay_receive, send)
