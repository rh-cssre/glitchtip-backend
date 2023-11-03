import json
import uuid

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.timezone import now
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from projects.models import Project

from .authentication import event_auth
from .schema import (
    BaseEventIngestSchema,
    CSPIssueEventSchema,
    EnvelopeHeaderSchema,
    EnvelopeSchema,
    ErrorIssueEventSchema,
    EventIngestSchema,
    InterchangeIssueEvent,
    IssueEventSchema,
    ItemHeaderSchema,
    SecuritySchema,
)
from .tasks import ingest_event, ingest_transaction

router = Router(auth=event_auth)


class EventIngestOut(Schema):
    event_id: str


class EnvelopeIngestOut(Schema):
    id: str


async def async_call_celery_task(task, *args):
    """
    Either dispatch the real celery task or run it with sync_to_async
    This can be used for testing or a celery-less operation.
    """
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return await sync_to_async(task.delay)(*args)
    else:
        return task.delay(*args)


def get_issue_event_class(event: BaseEventIngestSchema):
    return ErrorIssueEventSchema if event.exception else IssueEventSchema


@router.post("/{project_id}/store/", response=EventIngestOut)
async def event_store(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    """
    Event store is the original event ingest API from OSS Sentry but is used less often
    Unlike Envelope, it accepts only one Issue event.
    """
    received_at = now()
    issue_event_class = get_issue_event_class(payload)
    issue_event = InterchangeIssueEvent(
        event_id=payload.event_id,
        project_id=project_id,
        received_at=received_at,
        payload=issue_event_class(**payload.dict()),
    )
    await async_call_celery_task(ingest_event, issue_event.dict())
    return {"event_id": payload.event_id.hex}


@router.post("/{project_id}/envelope/", response=EnvelopeIngestOut)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    """
    Envelopes can contain various types of data.
    GlitchTip supports issue events and transaction events.
    Ignore other data types.
    Do support multiple valid events
    Make a few io calls as possible. Some language SDKs (PHP) cannot run async code
    and will block while waiting for GlitchTip to respond.
    """
    received_at = now()
    header = payload._header
    for item_header, item in payload._items:
        if item_header.type == "event":
            issue_event_class = get_issue_event_class(item)
            issue_event = InterchangeIssueEvent(
                event_id=header.event_id,
                project_id=project_id,
                received_at=received_at,
                payload=issue_event_class(**item.dict()),
            )
            await async_call_celery_task(ingest_event, issue_event.dict())
        elif item_header.type == "transaction":
            pass
            # ingest_transaction.delay(project_id, {})

    return {"id": header.event_id.hex}


@router.post("/{project_id}/security/")
async def event_security(
    request: HttpRequest,
    payload: SecuritySchema,
    project_id: int,
):
    """
    Accept Security (and someday other) issue events.
    Reformats event to make CSP browser format match more standard
    event format.
    """
    received_at = now()
    event = CSPIssueEventSchema(csp=payload.csp_report.dict(by_alias=True))
    issue_event = InterchangeIssueEvent(
        project_id=project_id,
        received_at=received_at,
        payload=event.dict(by_alias=True),
    )
    await async_call_celery_task(ingest_event, issue_event.dict(by_alias=True))
    return HttpResponse(status=201)
