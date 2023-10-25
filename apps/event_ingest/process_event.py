from typing import Union, Optional
from dataclasses import dataclass
from django.db import transaction
from django.db.models import Q
from django.db.utils import IntegrityError
from sentry.eventtypes.error import ErrorEvent
from sentry.culprit import generate_culprit

from apps.issue_events.models import IssueEventType, IssueHash, Issue, IssueEvent

from .schema import EventIngestSchema, EventMessage
from .utils import generate_hash


@dataclass
class ProcessingEvent:
    project_id: int
    event: EventIngestSchema
    issue_hash: str
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


def process_events(project_events: list[tuple[int, EventIngestSchema]]):
    # Collected/calculated event data while processing
    processing_events: list[ProcessingEvent] = []
    # Collect Q objects for bulk issue hash lookup
    q_objects = Q()
    for project_id, event in project_events:
        if event.exception:
            event_type = IssueEventType.ERROR
            sentry_event = ErrorEvent()
            # metadata = eventtype.get_metadata(data)
            title = "fake title"
            culprit = "fake culprit"
        elif not event.platform:
            event_type = IssueEventType.CSP
        else:
            event_type = IssueEventType.DEFAULT
            title = transform_message(event.message) if event.message else "<untitled>"
            culprit: str = (
                event.transaction
                if event.transaction
                else generate_culprit(event.dict())
            )
        issue_hash = generate_hash(title, culprit, event_type, event.fingerprint)
        processing_events.append(
            ProcessingEvent(
                project_id=project_id,
                event=event,
                issue_hash=issue_hash,
            )
        )
        q_objects |= Q(project_id=project_id, value=issue_hash)

    issue_defaults = {}
    hash_queryset = IssueHash.objects.filter(q_objects)
    issue_events: list[IssueEvent] = []
    for processing_event in processing_events:
        for hash_obj in hash_queryset:
            if hash_obj.value.hex == issue_hash and hash_obj.project_id == project_id:
                processing_event.issue_id = hash_obj.issue_id
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
                issue_id=processing_event.issue_id,
                data={},
            )
        )

    IssueEvent.objects.bulk_create(issue_events)
