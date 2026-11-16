import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_vtasks import task
from pydantic import ValidationError
from sentry_sdk import capture_exception

from apps.event_ingest.schema import (
    InterchangeTransactionEvent,
    IssueTaskMessage,
    UserReportTaskMessage,
)
from apps.issue_events.models import IssueEvent, UserReport
from glitchtip.partition_manager import UUID7Helper

from .process_event import process_issue_events, process_transaction_events

logger = logging.getLogger(__name__)


def _validate_batch(schema, tasks: list) -> list:
    """Validate each batched message independently, dropping (and reporting)
    any that fail instead of failing the whole batch.

    The ingest views bound what reaches the queue, but they validate more
    lightly than these schemas (deliberately so for the Rust ingest path),
    and a single poison message must not take down the other ~99 messages
    batched with it.
    """
    messages = []
    for t in tasks:
        try:
            messages.append(schema(**t["args"][0]))
        except ValidationError as e:
            capture_exception(e)
            logger.warning(
                "Dropped invalid %s ingest message", schema.__name__, exc_info=e
            )
    return messages


@task(queue_name="ingest")
async def ingest_event(tasks: list):
    logger.info(f"Process {len(tasks)} issue event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    await process_issue_events(
        _validate_batch(IssueTaskMessage, tasks),
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
async def ingest_transaction(tasks: list):
    logger.info(f"Process {len(tasks)} transaction event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    await process_transaction_events(
        _validate_batch(InterchangeTransactionEvent, tasks),
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
async def ingest_user_report(tasks: list):
    messages = _validate_batch(UserReportTaskMessage, tasks)

    # Parse each event_id to a UUID once, dropping (and reporting) any message
    # whose event_id is present but not a valid UUID. event_id is typed
    # ``str | None`` and feedback items forward ``associated_event_id`` raw, so
    # a non-UUID value (e.g. "12345") would raise ValueError here and fail the
    # whole batch — the per-message guard keeps one poison message from taking
    # down the other reports.
    parsed: list[tuple[UserReportTaskMessage, uuid.UUID | None]] = []
    for msg in messages:
        if msg.event_id:
            try:
                parsed.append((msg, uuid.UUID(msg.event_id)))
            except (ValueError, AttributeError, TypeError) as e:
                capture_exception(e)
                logger.warning("Dropped user report with invalid event_id", exc_info=e)
        else:
            parsed.append((msg, None))

    # Collect event_ids that need issue lookup
    event_id_map: dict[uuid.UUID, str | None] = {
        event_uuid: None for _, event_uuid in parsed if event_uuid is not None
    }

    # Bulk lookup: one query for all associated events
    if event_id_map:
        recent_lower = UUID7Helper.from_datetime(timezone.now() - timedelta(weeks=1))
        org_ids = {msg.organization_id for msg, eu in parsed if eu is not None}
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
    for msg, event_uuid in parsed:
        if event_uuid is not None:
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
