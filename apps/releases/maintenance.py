import logging
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import Deploy, Release, ReleaseProject

logger = logging.getLogger(__name__)


def cleanup_old_releases():
    days_ago = now() - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
    queryset = Release.objects.filter(created__lt=days_ago).order_by("id")

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:1000])
        if not batch_ids:
            break
        # Delete CASCADE'd FKs explicitly via _raw_delete to avoid collector overhead
        Deploy.objects.filter(release_id__in=batch_ids)._raw_delete(queryset.db)
        ReleaseProject.objects.filter(release_id__in=batch_ids)._raw_delete(queryset.db)
        count = Release.objects.filter(id__in=batch_ids)._raw_delete(queryset.db)
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old releases", total_deleted)
