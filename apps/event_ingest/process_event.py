from dataclasses import dataclass
from typing import Any, Optional, Union
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Q
from django.db.utils import IntegrityError

from alerts.models import Notification
from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent, IssueEventType, IssueHash
from sentry.culprit import generate_culprit

from .schema import (
    EventMessage,
    InterchangeIssueEvent,
)
from .utils import generate_hash


@dataclass
class ProcessingEvent:
    event: InterchangeIssueEvent
    issue_hash: str
    title: str
    event_data: dict[str, Any]
    issue_id: Optional[int] = None
    issue_created = False


def transform_message(message: Union[str, EventMessage]) -> str:
    if isinstance(message, str):
        return message
    if not message.formatted and message.message:
        params = message.params
        if isinstance(params, list):
            return message.message % tuple(params)
        elif isinstance(params, dict):
            return message.message.format(**params)
    return message.formatted


def process_issue_events(ingest_events: list[InterchangeIssueEvent]):
    """
    Accepts a list of events to ingest. Events should:
    - Few enough to save in a single DB call
    - Permission is already checked, these events are to write to the DB
    - Some invalid events are tolerated (ignored), including duplicate event id

    When there is an error in this function, care should be taken as to when to log,
    error, or ignore. If the SDK sends "weird" data, we want to log that.
    It's better to save a minimal event than to ignore it.
    """
    # Collected/calculated event data while processing
    processing_events: list[ProcessingEvent] = []
    # Collect Q objects for bulk issue hash lookup
    q_objects = Q()
    for ingest_event in ingest_events:
        event_data: dict[str, Any] = {}
        event = ingest_event.payload
        title = ""
        culprit = ""
        if event.type == IssueEventType.ERROR:
            # sentry_event = ErrorEvent()
            # metadata = eventtype.get_metadata(data)
            title = "fake title"
            culprit = "fake culprit"
        elif event.type == IssueEventType.CSP:
            humanized_directive = event.csp.effective_directive.replace("-src", "")
            uri = urlparse(event.csp.blocked_uri).netloc
            title = f"Blocked '{humanized_directive}' from '{uri}'"
            culprit = "fake culprit"
            event_data["csp"] = event.csp.dict()
        else:  # Default Event Type
            title = transform_message(event.message) if event.message else "<untitled>"
            culprit = (
                event.transaction
                if event.transaction
                else generate_culprit(event.dict())
            )
        issue_hash = generate_hash(title, culprit, event.type, event.fingerprint)
        processing_events.append(
            ProcessingEvent(
                event=ingest_event,
                issue_hash=issue_hash,
                title=title,
                event_data=event_data,
            )
        )
        q_objects |= Q(project_id=ingest_event.project_id, value=issue_hash)

    hash_queryset = IssueHash.objects.filter(q_objects).values(
        "value", "project_id", "issue_id", "issue__status"
    )
    issue_events: list[IssueEvent] = []
    issues_to_reopen = []
    for processing_event in processing_events:
        event_type = processing_event.event.payload.type
        project_id = processing_event.event.project_id
        issue_defaults = {
            "type": event_type,
            "title": processing_event.title,
        }
        for hash_obj in hash_queryset:
            if (
                hash_obj["value"].hex == issue_hash
                and hash_obj["project_id"] == project_id
            ):
                processing_event.issue_id = hash_obj["issue_id"]
                if hash_obj["issue__status"] == EventStatus.RESOLVED:
                    issues_to_reopen.append(hash_obj["issue_id"])
                break

        if not processing_event.issue_id:
            try:
                with transaction.atomic():
                    issue = Issue.objects.create(
                        project_id=project_id, **issue_defaults
                    )
                    IssueHash.objects.create(
                        issue=issue, value=issue_hash, project_id=project_id
                    )
                    processing_event.issue_id = issue.id
                    processing_event.issue_created = True
            except IntegrityError:
                processing_event.issue_id = IssueHash.objects.get(
                    project_id=project_id, value=issue_hash
                ).issue_id
        issue_events.append(
            IssueEvent(
                id=processing_event.event.event_id,
                date_received=processing_event.event.received_at,
                issue_id=processing_event.issue_id,
                type=event_type,
                data=processing_event.event_data,
            )
        )

    if issues_to_reopen:
        Issue.objects.filter(id__in=issues_to_reopen).update(
            status=EventStatus.UNRESOLVED
        )
        Notification.objects.filter(issues__in=issues_to_reopen).delete()

    # ignore_conflicts because we could have an invalid duplicate event_id
    IssueEvent.objects.bulk_create(issue_events, ignore_conflicts=True)
