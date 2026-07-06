"""
Lightweight ASGI dispatcher that routes ingest paths to a Django handler
with minimal middleware, bypassing session, auth, CSRF, messages, locale,
and allauth middleware on the hot ingest path.

Non-ingest requests pass through to the full Django application unchanged.
"""

import re

_INGEST_PATH_RE = re.compile(
    r"^/(?:"
    r"api/\d+/(?:envelope|store|minidump|security)/"
    # Native OTLP/HTTP ingest (optional trailing slash, no /api prefix).
    r"|v1/(?:logs|traces|metrics)/?$"
    r")"
)


class IngestDispatcher:
    """
    ASGI application that routes ingest requests to a Django handler with
    minimal middleware, and everything else to the full Django app.

    Ingest endpoints only need:
    - SecurityMiddleware (HSTS headers)
    - CorsMiddleware (browser SDKs send from different origins)

    Request-body decompression (gzip/deflate/br/zstd) is not a middleware:
    it runs in Rust (gt_rust) at each ingest endpoint's body-read seam.

    Skipped for ingest (saves ~8 middleware calls per request):
    - SessionMiddleware
    - CSP / XFrameOptions / Clickjacking
    - WhiteNoise
    - CommonMiddleware
    - CsrfViewMiddleware
    - AuthenticationMiddleware
    - MessageMiddleware
    - LocaleMiddleware
    - AccountMiddleware
    """

    def __init__(self, full_app):
        self._full_app = full_app
        self._ingest_app = None

    def _get_ingest_app(self):
        """Lazily build the ingest ASGI handler with minimal middleware."""
        if self._ingest_app is None:
            from django.core.handlers.asgi import ASGIHandler

            class IngestASGIHandler(ASGIHandler):
                _middleware_chain = None

                @classmethod
                def _get_middleware_setting(cls):
                    return [
                        # async-backend needs explicit per-request cleanup to
                        # return pool connections; without it the pool
                        # saturates after max_size requests.
                        "django_async_backend.middleware.close_async_connections",
                        "django.middleware.security.SecurityMiddleware",
                        "corsheaders.middleware.CorsMiddleware",
                    ]

                def load_middleware(self, is_async=True):
                    # Override to use our minimal middleware list
                    from django.conf import settings

                    original = settings.MIDDLEWARE
                    settings.MIDDLEWARE = self._get_middleware_setting()
                    try:
                        super().load_middleware(is_async=is_async)
                    finally:
                        settings.MIDDLEWARE = original

            self._ingest_app = IngestASGIHandler()
        return self._ingest_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _INGEST_PATH_RE.match(scope.get("path", "")):
            ingest_app = self._get_ingest_app()
            await ingest_app(scope, receive, send)
        else:
            await self._full_app(scope, receive, send)
