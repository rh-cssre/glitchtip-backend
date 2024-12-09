from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import DebugSymbolBundle


def cleanup_old_debug_symbol_bundles():
    days_ago = now() - timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)
    queryset = DebugSymbolBundle.objects.filter(last_used__lt=days_ago)
    queryset._raw_delete(queryset.db)
