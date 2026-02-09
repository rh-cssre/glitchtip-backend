import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_vtasks import task

from apps.event_ingest.schema import (
    InterchangeTransactionEvent,
    IssueTaskMessage,
    UserReportTaskMessage,
)
from apps.issue_events.models import IssueEvent, UserReport
from glitchtip.partition_manager import UUID7Helper

from .process_event import process_issue_events, process_transaction_events

logger = logging.getLogger(__name__)


@task(queue_name="ingest")
def ingest_event(tasks: list):
    logger.info(f"Process {len(tasks)} issue event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_issue_events(
        [IssueTaskMessage(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
def ingest_transaction(tasks: list):
    logger.info(f"Process {len(tasks)} transaction event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_transaction_events(
        [InterchangeTransactionEvent(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
async def ingest_user_report(tasks: list):
    messages = [UserReportTaskMessage(**t["args"][0]) for t in tasks]

    # Collect event_ids that need issue lookup
    event_id_map: dict[uuid.UUID, str | None] = {}
    for msg in messages:
        if msg.event_id:
            event_id_map[uuid.UUID(msg.event_id)] = None

    # Bulk lookup: one query for all associated events
    if event_id_map:
        recent_lower = UUID7Helper.from_datetime(
            timezone.now() - timedelta(weeks=1)
        )
        org_ids = {msg.organization_id for msg in messages if msg.event_id}
        async for evt in (
            IssueEvent.objects.filter(
                event_id__in=event_id_map.keys(),
                id__gte=recent_lower,
                organization_id__in=org_ids,
            )
            .only("event_id", "issue_id")
            .all()
        ):
            event_id_map[evt.event_id] = evt.issue_id

    # Build UserReport objects
    reports = []
    for msg in messages:
        if msg.event_id:
            event_uuid = uuid.UUID(msg.event_id)
            issue_id = event_id_map.get(event_uuid)
        else:
            event_uuid = uuid.uuid4()
            issue_id = None
        reports.append(
            UserReport(
                project_id=msg.project_id,
                issue_id=issue_id,
                event_id=event_uuid,
                name=msg.name,
                email=msg.email,
                comments=msg.comments,
            )
        )

    await UserReport.objects.abulk_create(reports, ignore_conflicts=True)
