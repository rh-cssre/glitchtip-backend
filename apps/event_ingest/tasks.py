import uuid

from django.conf import settings
from celery import shared_task
from celery_batches import Batches
from pydantic_core import ValidationError
from sentry_sdk import capture_exception, set_level

from glitchtip.celery import app

from .schema import EventIngestSchema
from .process_event import process_events

FLUSH_EVERY = 100
FLUSH_INTERVAL = 2


from functools import wraps


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_event(requests):
    project_events: tuple[int, list[EventIngestSchema]] = []
    for request in requests:
        try:
            project_events.append(
                (request.args[0], EventIngestSchema(**request.args[1]))
            )
        except ValidationError as err:
            set_level("warning")
            capture_exception(err)
    process_events(project_events)
    for request in requests:
        app.backend.mark_as_done(request.id, None, request=request)


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_transaction(requests):
    pass
