# Load webhook handler during app startup
from django.conf import settings

if not settings.IS_CELERY:
    from .hooks import update_subscription  # noqa: F401
