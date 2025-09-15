from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from apps.issue_events.constants import LogLevel

from .schema import (
    CSPIssueEventSchema,
    ErrorIssueEventSchema,
    IssueEventSchema,
)


@dataclass(frozen=True)
class IngestTaskMessage:
    """A simple, type-hinted data container for a validated event
    being sent to a Celery worker.
    """

    project_id: int
    organization_id: int
    received: datetime
    payload: dict  # Presumed to be validated prior


@dataclass
class ProcessingEvent:
    project_id: int
    organization_id: int
    received: datetime
    payload: IssueEventSchema | ErrorIssueEventSchema | CSPIssueEventSchema
    issue_hash: str
    title: str
    transaction: str
    metadata: dict[str, Any]
    event_data: dict[str, Any]
    event_tags: dict[str, str]
    level: LogLevel | None = None
    issue_id: int | None = None
    issue_created = False
    release_id: int | None = None


@dataclass
class IssueUpdate:
    last_seen: datetime
    search_vector: str
    added_count: int = 1


class IssueStats(TypedDict):
    count: int
    organization_id: int | None
