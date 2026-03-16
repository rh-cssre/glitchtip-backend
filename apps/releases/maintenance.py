import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.utils.timezone import now

from .models import Deploy, Release, ReleaseProject

logger = logging.getLogger(__name__)


def cleanup_old_releases():
    from apps.issue_events.models import Issue, IssueEvent
    from apps.sourcecode.models import DebugSymbolBundle

    days_ago = now() - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    queryset = Release.objects.using(db_alias).filter(created__lt=days_ago).order_by("id")

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:1000])
        if not batch_ids:
            break
        # Nullify SET_NULL FK references before deleting releases.
        # _raw_delete() bypasses Django's collector, so SET_NULL doesn't fire.
        Issue.objects.filter(first_release_id__in=batch_ids).using(db_alias).update(
            first_release=None
        )
        Issue.objects.filter(last_release_id__in=batch_ids).using(db_alias).update(
            last_release=None
        )
        Issue.objects.filter(resolved_in_release_id__in=batch_ids).using(
            db_alias
        ).update(resolved_in_release=None)
        IssueEvent.objects.filter(release_id__in=batch_ids).using(db_alias).update(
            release=None
        )
        DebugSymbolBundle.objects.filter(release_id__in=batch_ids).using(
            db_alias
        ).update(release=None)
        # Delete CASCADE'd FKs explicitly via _raw_delete to avoid collector overhead
        Deploy.objects.filter(release_id__in=batch_ids)._raw_delete(db_alias)
        ReleaseProject.objects.filter(release_id__in=batch_ids)._raw_delete(db_alias)
        # A concurrent ingest task may re-create a ReleaseProject between
        # the delete above and this delete (TOCTOU race). Skip and retry
        # on the next maintenance run.
        try:
            count = Release.objects.filter(id__in=batch_ids)._raw_delete(db_alias)
        except IntegrityError:
            logger.info("Skipped release batch due to concurrent FK insert")
            continue
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old releases", total_deleted)
