import glitchtip.lookups  # noqa: F401

from .celery import app as celery_app

__all__ = ("celery_app",)
