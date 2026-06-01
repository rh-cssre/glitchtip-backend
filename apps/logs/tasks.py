import logging
from dataclasses import dataclass
from datetime import datetime

from django_vtasks import task

from .process_logs import process_log_events

logger = logging.getLogger(__name__)


@dataclass
class LogTaskMessage:
    """Deserialized log ingest message for task processing."""

    project_id: int
    organization_id: int
    received: datetime
    logs: list[dict]


@task(queue_name="ingest")
async def ingest_logs(tasks: list):
    """Process batched log ingestion tasks."""
    logger.info(f"Processing {len(tasks)} log batch requests")

    messages = []
    for task_data in tasks:
        args = task_data["args"][0]
        messages.append(
            LogTaskMessage(
                project_id=args["project_id"],
                organization_id=args["organization_id"],
                received=datetime.fromisoformat(args["received"]),
                logs=args["logs"],
            )
        )

    await process_log_events(messages)
