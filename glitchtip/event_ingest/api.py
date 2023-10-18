import json
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from projects.models import Project

from .authentication import event_auth, get_project
from .data_models import (
    EnvelopeSchema,
    EventIngestSchema,
    EnvelopeHeaderSchema,
    EnvelopeEventIngestSchema,
    ItemHeaderSchema,
)
from .tasks import ingest_event, ingest_transaction

router = Router()


class EventIngestOut(Schema):
    event_id: str


class EnvelopeIngestOut(Schema):
    id: str


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


@router.post("/{project_id}/envelope/", response=EnvelopeIngestOut, auth=event_auth)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    # GlitchTip supports only envelopes with a header, item header, event|transaction
    # Validate this and reject anything else
    if not len(payload) >= 3:
        raise ValidationError([{"message": "Envelope too small"}])
    header = payload[0] if isinstance(payload[0], EnvelopeHeaderSchema) else None
    item_header = payload[1] if isinstance(payload[1], ItemHeaderSchema) else None

    if not header or not item_header:
        raise ValidationError([{"message": "Envelope contains no usable data"}])

    event_id = header.event_id
    if item_header.type == "event":
        event = (
            payload[2] if isinstance(payload[2], EnvelopeEventIngestSchema) else None
        )
        if not event:
            raise ValidationError([{"message": "Envelope not valid"}])
        ingest_event.delay(project_id, event.dict())
    else:  # transaction
        ingest_transaction.delay(project_id, {})

    if not hasattr(header, "event_id"):
        raise ValidationError([])

    return {"id": event_id.hex}


@router.post("/{project_id}/security/", response=EventIngestOut)
async def event_security(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    check_status()
