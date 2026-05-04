"""
WSGI config for glitchtip project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application
from uwsgi_chunked import Chunked

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
