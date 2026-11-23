from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils.timezone import now

from .models import DebugSymbolBundle


async def cleanup_old_debug_symbol_bundles():
    days_ago = now() - timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    queryset = DebugSymbolBundle.objects.using(db_alias).filter(last_used__lt=days_ago)
    await sync_to_async(queryset._raw_delete)(db_alias)
