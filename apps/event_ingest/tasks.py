import logging

from django.conf import settings
from django_vtasks import task

from apps.event_ingest.schema import InterchangeTransactionEvent, IssueTaskMessage

from .process_event import process_issue_events, process_transaction_events

logger = logging.getLogger(__name__)


@task(queue_name="ingest")
def ingest_event(tasks: list):
    logger.info(f"Process {len(tasks)} issue event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_issue_events(
        [IssueTaskMessage(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
def ingest_transaction(tasks: list):
    logger.info(f"Process {len(tasks)} transaction event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_transaction_events(
        [InterchangeTransactionEvent(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )
