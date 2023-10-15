import uuid

from celery import shared_task

from .data_models import EventIngestSchema


@shared_task
def ingest_event(project_id: int, data: dict):
    event = EventIngestSchema(**data)
    print(event)
