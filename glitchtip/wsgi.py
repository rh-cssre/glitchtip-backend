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
