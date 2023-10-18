import uuid

from celery import shared_task
from celery_batches import Batches

from .data_models import EventIngestSchema


@shared_task(base=Batches, flush_every=100, flush_interval=2)
def ingest_event(requests):
    print(requests)
    print(requests[0].args)
    event = EventIngestSchema(**data)
    print(event)


@shared_task(base=Batches, flush_every=100, flush_interval=2)
def ingest_transaction(requests):
    pass
