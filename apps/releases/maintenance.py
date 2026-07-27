import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError
from django.utils.timezone import now

from .models import Deploy, Release, ReleaseProject

logger = logging.getLogger(__name__)


async def cleanup_old_releases():
    from apps.issue_events.models import Issue, IssueEvent, IssueIndex
    from apps.sourcecode.models import DebugSymbolBundle

    days_ago = now() - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    queryset = (
        Release.objects.using(db_alias).filter(created__lt=days_ago).order_by("id")
    )

    total_deleted = 0
    while True:
        batch_ids = await sync_to_async(list)(
            queryset.values_list("id", flat=True)[:1000]
        )
        if not batch_ids:
            break
        # Nullify SET_NULL FK references before deleting releases.
        # _raw_delete() bypasses Django's collector, so SET_NULL doesn't fire.
        await (
            Issue.objects.filter(first_release_id__in=batch_ids)
            .using(db_alias)
            .aupdate(first_release=None)
        )
        # last_release moved to the IssueIndex leaf (its FK is DO_NOTHING,
        # so nullify here before the raw release delete).
        await (
            IssueIndex.objects.filter(last_release_id__in=batch_ids)
            .using(db_alias)
            .aupdate(last_release=None)
        )
        await (
            Issue.objects.filter(resolved_in_release_id__in=batch_ids)
            .using(db_alias)
            .aupdate(resolved_in_release=None)
        )
        await (
            IssueEvent.objects.filter(release_id__in=batch_ids)
            .using(db_alias)
            .aupdate(release=None)
        )
        await (
            DebugSymbolBundle.objects.filter(release_id__in=batch_ids)
            .using(db_alias)
            .aupdate(release=None)
        )
        # Delete CASCADE'd FKs explicitly via _raw_delete to avoid collector overhead
        await sync_to_async(
            Deploy.objects.filter(release_id__in=batch_ids)._raw_delete
        )(db_alias)
        await sync_to_async(
            ReleaseProject.objects.filter(release_id__in=batch_ids)._raw_delete
        )(db_alias)
        # A concurrent ingest task may re-create a ReleaseProject between
        # the delete above and this delete (TOCTOU race). Stop and let
        # the next maintenance run pick up where we left off — continuing
        # would re-select the same undeletable batch and loop forever.
        try:
            count = await sync_to_async(
                Release.objects.filter(id__in=batch_ids)._raw_delete
            )(db_alias)
        except IntegrityError:
            logger.info("Skipped release batch due to concurrent FK insert")
            break
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old releases", total_deleted)
