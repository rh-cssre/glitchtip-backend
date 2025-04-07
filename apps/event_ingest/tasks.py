import logging

from celery import shared_task

from celery_batches import Batches
from glitchtip.celery import app

from .process_event import process_issue_events, process_transaction_events
from .schema import InterchangeIssueEvent

logger = logging.getLogger(__name__)

FLUSH_EVERY = 100
FLUSH_INTERVAL = 2


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_event(requests):
    logger.info(f"Process {len(requests)} issue event requests")
    process_issue_events(
        [InterchangeIssueEvent(**request.args[0]) for request in requests]
    )
    [app.backend.mark_as_done(request.id, None, request) for request in requests]


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_transaction(requests):
    logger.info(f"Process {len(requests)} transaction event requests")
    process_transaction_events(
        [InterchangeIssueEvent(**request.args[0]) for request in requests]
    )
    [app.backend.mark_as_done(request.id, None, request) for request in requests]
