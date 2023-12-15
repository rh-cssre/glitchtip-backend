from typing import Optional

from anonymizeip import anonymize_ip
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpResponse
from ipware import get_client_ip
from ninja import Router, Schema

from .authentication import EventAuthHttpRequest, event_auth
from .schema import (
    CSPIssueEventSchema,
    EnvelopeSchema,
    ErrorIssueEventSchema,
    EventIngestSchema,
    EventUser,
    IngestIssueEvent,
    InterchangeIssueEvent,
    IssueEventSchema,
    SecuritySchema,
)
from .tasks import ingest_event

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


def get_issue_event_class(event: IngestIssueEvent):
    return ErrorIssueEventSchema if event.exception else IssueEventSchema


def get_ip_address(request: EventAuthHttpRequest) -> Optional[str]:
    """
    Get IP address from request. Anonymize it based on project settings.
    Keep this logic in the api view, we aim to anonymize data before storing
    on redis/postgres.
    """
    project = request.auth
    client_ip, is_routable = get_client_ip(request)
    if is_routable:
        if project.should_scrub_ip_addresses:
            client_ip = anonymize_ip(client_ip)
        return client_ip
    return None


@router.post("/{project_id}/store/", response=EventIngestOut)
async def event_store(
    request: EventAuthHttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    """
    Event store is the original event ingest API from OSS Sentry but is used less often
    Unlike Envelope, it accepts only one Issue event.
    """
    if client_ip := get_ip_address(request):
        if payload.user:
            payload.user.ip_address = client_ip
        else:
            payload.user = EventUser(ip_address=client_ip)

    issue_event_class = get_issue_event_class(payload)
    issue_event = InterchangeIssueEvent(
        event_id=payload.event_id,
        project_id=project_id,
        payload=issue_event_class(**payload.dict()),
    )
    await async_call_celery_task(ingest_event, issue_event.dict())
    return {"event_id": payload.event_id.hex}


@router.post("/{project_id}/envelope/", response=EnvelopeIngestOut)
async def event_envelope(
    request: EventAuthHttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    """
    Envelopes can contain various types of data.
    GlitchTip supports issue events and transaction events.
    Ignore other data types.
    Do support multiple valid events
    Make as few io calls as possible. Some language SDKs (PHP) cannot run async code
    and will block while waiting for GlitchTip to respond.
    """
    client_ip = get_ip_address(request)

    header = payload._header
    for item_header, item in payload._items:
        if item_header.type == "event":
            if item.user:
                item.user.ip_address = client_ip
            else:
                item.user = EventUser(ip_address=client_ip)
            issue_event_class = get_issue_event_class(item)
            issue_event = InterchangeIssueEvent(
                event_id=header.event_id,
                project_id=project_id,
                payload=issue_event_class(**item.dict()),
            )
            await async_call_celery_task(ingest_event, issue_event.dict())
        elif item_header.type == "transaction":
            pass
            # ingest_transaction.delay(project_id, {})

    return {"id": header.event_id.hex}


@router.post("/{project_id}/security/")
async def event_security(
    request: EventAuthHttpRequest,
    payload: SecuritySchema,
    project_id: int,
):
    """
    Accept Security (and someday other) issue events.
    Reformats event to make CSP browser format match more standard
    event format.
    """
    event = CSPIssueEventSchema(csp=payload.csp_report.dict(by_alias=True))
    if client_ip := get_ip_address(request):
        if event.user:
            event.user.ip_address = client_ip
        else:
            event.user = EventUser(ip_address=client_ip)
    issue_event = InterchangeIssueEvent(
        project_id=project_id,
        payload=event.dict(by_alias=True),
    )
    await async_call_celery_task(ingest_event, issue_event.dict(by_alias=True))
    return HttpResponse(status=201)
