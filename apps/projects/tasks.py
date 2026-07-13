import logging

from asgiref.sync import sync_to_async
from django.tasks import task

from apps.issue_events.maintenance import (
    delete_issues_in_batches,
    raw_delete_in_batches,
)
from apps.issue_events.models import IssueEvent
from apps.logs.models import LogEvent

from .models import Project

logger = logging.getLogger(__name__)


@task
async def delete_project(project_id: int):
    project = await Project.objects.select_related("organization").aget(id=project_id)
    org_id = project.organization_id

    # Rewrite cold storage Parquet files to exclude this project's data
    await sync_to_async(_rewrite_cold_storage_for_project)(project)

    # Batch-delete from partitioned tables to keep lock counts low.
    await raw_delete_in_batches(
        IssueEvent.objects.filter(organization_id=org_id, issue__project=project)
    )
    await raw_delete_in_batches(
        LogEvent.objects.filter(organization_id=org_id, project=project)
    )
    await delete_issues_in_batches(project.issues.all())

    # Remaining relations are non-partitioned — safe for Django cascade.
    await sync_to_async(project.force_delete)()
    logger.info(
        "Project %s (id=%s, org=%s) fully deleted", project.name, project_id, org_id
    )


def _rewrite_cold_storage_for_project(project: Project):
    from apps.issue_events.models import Issue
    from glitchtip.cold_storage import (
        is_duckdb_available,
        rewrite_parquet_excluding_project,
    )

    if not is_duckdb_available():
        return

    org_id = project.organization_id

    # Rewrite logs Parquet files (has project_id column)
    rewrite_parquet_excluding_project(
        org_id=org_id,
        project_id=project.id,
        storage_prefix="logs_logevent",
    )

    # Rewrite performance span Parquet — raw spans and the trend rollups
    # (both carry project_id). Without the rollup pass a deleted project's
    # aggregates would survive for the long rollup retention.
    rewrite_parquet_excluding_project(
        org_id=org_id,
        project_id=project.id,
        storage_prefix="performance_spans",
    )
    rewrite_parquet_excluding_project(
        org_id=org_id,
        project_id=project.id,
        storage_prefix="performance_spans_rollup",
    )

    # Rewrite issue event Parquet files (has issue_id, not project_id)
    # Pre-fetch issue IDs while Issues still exist in DB
    issue_ids = list(Issue.objects.filter(project=project).values_list("id", flat=True))
    if issue_ids:
        rewrite_parquet_excluding_project(
            org_id=org_id,
            issue_ids=issue_ids,
            storage_prefix="issue_events_issueevent",
        )
