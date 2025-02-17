from django.apps import AppConfig
from django.conf import settings


class DjstripeExtAppConfig(AppConfig):
    name = 'apps.djstripe_ext'

    def ready(self):
        if not settings.IS_CELERY:
            from .hooks import update_subscription  # noqa: F401