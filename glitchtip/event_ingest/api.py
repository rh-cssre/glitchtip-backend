import json
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from projects.models import Project

from .authentication import event_auth, get_project
from .data_models import EnvelopeSchema, EventIngestSchema
from .tasks import ingest_event

router = Router()


class EventIngestOut(Schema):
    event_id: str


def check_status():
    if settings.EVENT_STORE_DEBUG:
        print(json.dumps(self.request.data))


@router.post("/{project_id}/store/", response=EventIngestOut)
async def event_store(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
    project = await get_project(request)
    ingest_event.delay(project.id, payload.dict())
    return {"event_id": payload.event_id.hex}


@router.post("/{project_id}/envelope/", response=EventIngestOut, auth=event_auth)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    if not len(payload) >= 3:
        raise ValidationError([])
    header = payload[0]
    event = payload[2]
    if not hasattr(header, "event_id"):
        raise ValidationError([])

    event_id = header.event_id
    ingest_event.delay(project_id, event.dict())

    return {"event_id": event_id.hex}


@router.post("/{project_id}/security/", response=EventIngestOut)
async def event_security(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
