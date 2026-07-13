"""
WSGI config for glitchtip project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/wsgi/

============================================================================
DEPRECATED — WSGI SUPPORT WILL BE REMOVED IN AN UPCOMING RELEASE.
============================================================================

GlitchTip is async-first. Running under WSGI (Granian WSGI, uWSGI,
gunicorn sync workers, etc.) is no longer supported and will be removed
soon. Do not deploy new installs against this entrypoint; migrate existing
deployments to ASGI (``glitchtip.asgi:application``) under Granian, which
is the ASGI server shipped in our Docker image.

Concrete problems if you keep using WSGI:

* Every request that touches an async view (which now includes the entire
  ingest hot path: envelope, store, security, minidump) is bridged via
  ``async_to_sync``. That spins up a fresh event loop and a new task-local
  async-backend DB connection per request. The connection cannot be closed
  synchronously from ``__del__``, so sockets leak until the worker process
  exits and the pool saturates fast under load.
* The ingest fast-path dispatcher in ``glitchtip.ingest_asgi`` is bypassed
  entirely. Ingest requests pay the cost of the full Django middleware
  chain (auth, sessions, CSRF, locale, allauth, etc.) on every event.
* Performance is strictly worse on every axis vs. ASGI, and the gap is
  widening as more code paths move to native async.

If a custom deployment is importing this module directly: switch to
``glitchtip.asgi:application``. There is no supported migration path that
keeps WSGI.
============================================================================
"""

import os
import warnings

from django.core.wsgi import get_wsgi_application
from uwsgi_chunked import Chunked

warnings.warn(
    "glitchtip.wsgi is deprecated and will be removed in an upcoming release. "
    "GlitchTip is async-first; WSGI deployments leak DB connections per "
    "request, bypass the ingest fast path, and are strictly slower than "
    "ASGI. Switch to glitchtip.asgi:application under Granian (the ASGI "
    "server shipped in our Docker image).",
    DeprecationWarning,
    stacklevel=2,
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

application = Chunked(get_wsgi_application())

# Wrap application with granian proxy headers support
# This allows granian to properly handle X-Forwarded-For and X-Forwarded-Proto headers
# when running behind a reverse proxy (nginx, traefik, k8s ingress, etc.)
try:
    from granian.utils.proxies import wrap_wsgi_with_proxy_headers

    # TRUSTED_PROXIES can be:
    # - "*" to trust all proxies (default, safe when granian isn't directly exposed)
    # - A comma-separated list of IPs/CIDR ranges (e.g., "10.0.0.0/8,172.16.0.0/12")
    # - For k8s: "*" is recommended since pod IPs are dynamic and pods aren't exposed
    trusted_proxies = os.environ.get("TRUSTED_PROXIES", "*")
    if "," in trusted_proxies:
        trusted_proxies = [ip.strip() for ip in trusted_proxies.split(",")]

    application = wrap_wsgi_with_proxy_headers(
        application, trusted_hosts=trusted_proxies
    )
except ImportError:
    # Graceful fallback if granian can't be imported (custom installations, etc.)
    # The wrapper is safe to use with uwsgi but this provides defensive error handling
    pass
