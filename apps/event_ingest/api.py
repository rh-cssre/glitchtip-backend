import json
import uuid
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError

from projects.models import Project

from .authentication import event_auth, get_project
from .schema import (
    EnvelopeEventIngestSchema,
    EnvelopeHeaderSchema,
    EnvelopeSchema,
    EventIngestSchema,
    ItemHeaderSchema,
)
from .tasks import ingest_event, ingest_transaction

router = Router()


class EventIngestOut(Schema):
    event_id: str


class EnvelopeIngestOut(Schema):
    id: str


async def async_call_celery_task(task, *args):
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return await sync_to_async(task.delay)(*args)
    else:
        return task.delay(*args)


@router.post("/{project_id}/store/", response=EventIngestOut, auth=event_auth)
async def event_store(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    await async_call_celery_task(ingest_event, project_id, payload.dict())
    return {"event_id": payload.event_id.hex}


@router.post("/{project_id}/envelope/", response=EnvelopeIngestOut, auth=event_auth)
async def event_envelope(
    request: HttpRequest,
    payload: EnvelopeSchema,
    project_id: int,
):
    header = payload._header
    for item_header, item in payload._items:
        if item_header.type == "event":
            ingest_event.delay(project_id, item.dict())
        elif item_header.type == "transaction":
            pass
            # ingest_transaction.delay(project_id, {})

    return {"id": header.event_id.hex}


@router.post("/{project_id}/security/", response=EventIngestOut, auth=event_auth)
async def event_security(
    request: HttpRequest,
    payload: EventIngestSchema,
    project_id: int,
):
    pass
