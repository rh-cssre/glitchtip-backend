from dataclasses import asdict

from anonymizeip import anonymize_ip
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone
from ipware import get_client_ip
from ninja import Router, Schema
from ninja.errors import ValidationError

from apps.event_ingest.interfaces import IngestTaskMessage
from apps.issue_events.constants import IssueEventType
from glitchtip.partition_manager import UUID7Helper

from .authentication import EventAuthHttpRequest, event_auth
from .pii_scrubber import resolve_scrubber
from .schema import (
    CSPIssueEventSchema,
    EventIngestSchema,
    EventUser,
    SecuritySchema,
)
from .tasks import ingest_event
from .utils import serialize_for_vtasks

router = Router(auth=event_auth)


class EventIngestOut(Schema):
    event_id: str
    task_id: str | None = None  # For debug purposes only


class EnvelopeIngestOut(Schema):
    id: str | None = None


def get_ip_address(request: EventAuthHttpRequest) -> str | None:
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
    if await cache.aadd("uuid" + payload.event_id.hex, True) is False:
        raise ValidationError([{"message": "Duplicate event id"}])

    if client_ip := get_ip_address(request):
        if payload.user:
            payload.user.ip_address = client_ip
        else:
            payload.user = EventUser(ip_address=client_ip)

    issue_type = IssueEventType.ERROR if payload.exception else IssueEventType.DEFAULT
    scrubber = resolve_scrubber(
        request.auth.scrub_config, settings.GLITCHTIP_PII_SCRUB_DEFAULT
    )
    primary_id = UUID7Helper.from_datetime()
    issue_event = IngestTaskMessage(
        project_id=project_id,
        organization_id=request.auth.organization_id,
        payload=scrubber.scrub_event(payload.dict() | {"type": issue_type}),
        received=timezone.now(),
        update_first_event=request.auth.first_event is None,
        uuid=primary_id.hex,
    )
    await ingest_event.aenqueue(serialize_for_vtasks(asdict(issue_event)))
    return {"event_id": payload.event_id.hex}


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
    scrubber = resolve_scrubber(
        request.auth.scrub_config, settings.GLITCHTIP_PII_SCRUB_DEFAULT
    )
    primary_id = UUID7Helper.from_datetime()
    issue_event = IngestTaskMessage(
        project_id=project_id,
        organization_id=request.auth.organization_id,
        payload=scrubber.scrub_event(event.dict(by_alias=True)),
        received=timezone.now(),
        update_first_event=request.auth.first_event is None,
        uuid=primary_id.hex,
    )
    await ingest_event.aenqueue(serialize_for_vtasks(asdict(issue_event)))
    return HttpResponse(status=201)
