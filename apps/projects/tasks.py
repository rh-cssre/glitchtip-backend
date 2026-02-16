import logging

from asgiref.sync import sync_to_async
from django.tasks import task

from .models import Project

logger = logging.getLogger(__name__)


@task
async def delete_project(project_id: int):
    project = await Project.objects.select_related("organization").aget(id=project_id)
    org_id = project.organization_id

    # Rewrite cold storage Parquet files to exclude this project's data
    await sync_to_async(_rewrite_cold_storage_for_project)(project)

    await sync_to_async(project.force_delete)()
    logger.info(
        "Project %s (id=%s, org=%s) fully deleted", project.name, project_id, org_id
    )


def _rewrite_cold_storage_for_project(project: Project):
    from apps.issue_events.cold_storage import ISSUE_EVENT_EXPORT_COLUMN_TYPES
    from apps.issue_events.models import Issue
    from apps.logs.cold_storage import EXPORT_COLUMN_TYPES as LOG_COLUMN_TYPES
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
        table_name="logs_logevent",
        column_types=LOG_COLUMN_TYPES,
    )

    # Rewrite issue event Parquet files (has issue_id, not project_id)
    # Pre-fetch issue IDs while Issues still exist in DB
    issue_ids = list(Issue.objects.filter(project=project).values_list("id", flat=True))
    if issue_ids:
        rewrite_parquet_excluding_project(
            org_id=org_id,
            issue_ids=issue_ids,
            table_name="issue_events_issueevent",
            column_types=ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        )
