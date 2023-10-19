import uuid

from celery import shared_task
from celery_batches import Batches
from pydantic_core import ValidationError

from .schema import EventIngestSchema

FLUSH_EVERY = 100
FLUSH_INTERVAL = 2


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_event(requests):
    project_events: tuple[int, list[EventIngestSchema]] = []
    for request in requests:
        try:
            project_events.append(
                (request.args[0], EventIngestSchema(**request.args[1]))
            )
        except ValidationError:
            # Log this!!
            pass
    print(project_events)


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_transaction(requests):
    pass
