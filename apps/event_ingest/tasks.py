import uuid

from django.conf import settings
from celery import shared_task
from celery_batches import Batches
from pydantic_core import ValidationError

from glitchtip.celery import app

from .schema import InterchangeIssueEvent
from .process_event import process_issue_events

FLUSH_EVERY = 100
FLUSH_INTERVAL = 2


from functools import wraps


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_event(requests):
    print(f"Process {len(requests)} requests")
    process_issue_events(
        [InterchangeIssueEvent(**request.args[0]) for request in requests]
    )
    [app.backend.mark_as_done(request.id, None, request) for request in requests]


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_transaction(requests):
    pass
