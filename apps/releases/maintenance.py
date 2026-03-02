import logging
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import Deploy, Release, ReleaseProject

logger = logging.getLogger(__name__)


def cleanup_old_releases():
    from apps.issue_events.models import Issue, IssueEvent
    from apps.sourcecode.models import DebugSymbolBundle

    days_ago = now() - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
    queryset = Release.objects.filter(created__lt=days_ago).order_by("id")

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:1000])
        if not batch_ids:
            break
        # Nullify SET_NULL FK references before deleting releases.
        # _raw_delete() bypasses Django's collector, so SET_NULL doesn't fire.
        Issue.objects.filter(first_release_id__in=batch_ids).update(first_release=None)
        Issue.objects.filter(last_release_id__in=batch_ids).update(last_release=None)
        Issue.objects.filter(resolved_in_release_id__in=batch_ids).update(
            resolved_in_release=None
        )
        IssueEvent.objects.filter(release_id__in=batch_ids).update(release=None)
        DebugSymbolBundle.objects.filter(release_id__in=batch_ids).update(release=None)
        # Delete CASCADE'd FKs explicitly via _raw_delete to avoid collector overhead
        Deploy.objects.filter(release_id__in=batch_ids)._raw_delete(queryset.db)
        ReleaseProject.objects.filter(release_id__in=batch_ids)._raw_delete(queryset.db)
        count = Release.objects.filter(id__in=batch_ids)._raw_delete(queryset.db)
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old releases", total_deleted)
