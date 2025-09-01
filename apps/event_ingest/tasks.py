import logging

from celery import shared_task
from celery_batches import Batches

from apps.event_ingest.schema import InterchangeTransactionEvent, IssueTaskMessage
from glitchtip.celery import app

from .process_event import process_issue_events, process_transaction_events

logger = logging.getLogger(__name__)

FLUSH_EVERY = 100
FLUSH_INTERVAL = 2


def ingest_event(requests: list):  # type: ignore
    logger.info(f"Process {len(requests)} issue event requests")
    process_issue_events([IssueTaskMessage(**request.args[0]) for request in requests])
    [app.backend.mark_as_done(request.id, None, request) for request in requests]


ingest_event: Batches = shared_task(  # type: ignore Shadow done for type hinting
    ingest_event, base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL
)


@shared_task(base=Batches, flush_every=FLUSH_EVERY, flush_interval=FLUSH_INTERVAL)
def ingest_transaction(requests: list):
    logger.info(f"Process {len(requests)} transaction event requests")
    process_transaction_events(
        [InterchangeTransactionEvent(**request.args[0]) for request in requests]
    )
    [app.backend.mark_as_done(request.id, None, request) for request in requests]
