from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Union
from urllib.parse import urlparse

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.db.utils import IntegrityError
from ninja import Schema
from user_agents import parse

from alerts.models import Notification
from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent, IssueEventType, IssueHash
from sentry.culprit import generate_culprit
from sentry.eventtypes.error import ErrorEvent
from sentry.utils.strings import truncatechars

from ..shared.schema.contexts import (
    BrowserContext,
    ContextsSchema,
    DeviceContext,
    OSContext,
)
from .model_functions import PipeConcat
from .schema import IngestIssueEvent, InterchangeIssueEvent
from .utils import generate_hash, transform_parameterized_message


@dataclass
class ProcessingEvent:
    event: InterchangeIssueEvent
    issue_hash: str
    title: str
    transaction: str
    metadata: dict[str, Any]
    event_data: dict[str, Any]
    event_tags: dict[str, str]
    issue_id: Optional[int] = None
    issue_created = False


@dataclass
class IssueUpdate:
    last_seen: datetime
    added_count: int = 1
    search_vector: str = ""


def update_issues(processing_events: list[ProcessingEvent]):
    """
    Update any existing issues based on new statistics
    """
    issues_to_update: dict[int, IssueUpdate] = {}
    for processing_event in processing_events:
        if processing_event.issue_created:
            break

        issue_id = processing_event.issue_id
        if issue_id in issues_to_update:
            issues_to_update[issue_id].added_count += 1
            issues_to_update[issue_id].search_vector += f" {processing_event.title}"
            if issues_to_update[issue_id].last_seen < processing_event.event.received:
                issues_to_update[issue_id].last_seen = processing_event.event.received
        elif issue_id:
            issues_to_update[issue_id] = IssueUpdate(
                last_seen=processing_event.event.received
            )

    for issue_id, value in issues_to_update.items():
        Issue.objects.filter(id=issue_id).update(
            count=F("count") + value.added_count,
            search_vector=SearchVector(
                PipeConcat(F("search_vector"), SearchVector(Value(value.search_vector)))
            ),
            last_seen=Greatest(F("last_seen"), value.last_seen),
        )


def devalue(obj: Union[Schema, list]) -> Optional[Union[dict, list]]:
    """
    Convert Schema like {"values": []} into list or dict without unnecessary 'values'
    """
    if isinstance(obj, Schema) and hasattr(obj, "values"):
        return obj.dict(mode="json", exclude_none=True, exclude_defaults=True)["values"]
    elif isinstance(obj, list):
        return [
            x.dict(mode="json", exclude_none=True, exclude_defaults=True) for x in obj
        ]
    return None


def generate_contexts(event: IngestIssueEvent) -> ContextsSchema:
    """
    Add additional contexts if they aren't already set
    """
    contexts = event.contexts if event.contexts else ContextsSchema(root={})

    if request := event.request:
        if isinstance(request.headers, list):
            if ua_string := next(
                (x[1] for x in request.headers if x[0] == "User-Agent"), None
            ):
                user_agent = parse(ua_string)
                if "browser" not in contexts.root:
                    contexts.root["browser"] = BrowserContext(
                        name=user_agent.browser.family,
                        version=user_agent.browser.version_string,
                    )
                if "os" not in contexts.root:
                    contexts.root["os"] = OSContext(
                        name=user_agent.os.family, version=user_agent.os.version_string
                    )
                if "device" not in contexts.root:
                    device = user_agent.device
                    contexts.root["device"] = DeviceContext(
                        family=device.family,
                        model=device.model,
                        brand=device.brand,
                    )
    return contexts


def generate_tags(event: IngestIssueEvent) -> dict[str, str]:
    """Generate key-value tags based on context and other event data"""
    tags: dict[str, Optional[str]] = event.tags if isinstance(event.tags, dict) else {}

    if contexts := event.contexts:
        if browser := contexts.root.get("browser"):
            if isinstance(browser, BrowserContext):
                tags["browser.name"] = browser.name
                tags["browser"] = f"{browser.name} {browser.version}"
        if os := contexts.root.get("os"):
            if isinstance(os, OSContext):
                tags["os.name"] = os.name
        if device := contexts.root.get("device"):
            if isinstance(device, DeviceContext) and device.model:
                tags["device"] = device.model

    if user := event.user:
        if user.id:
            tags["user.id"] = user.id
        if user.email:
            tags["user.email"] = user.email
        if user.username:
            tags["user.username"] = user.username

    if environment := event.environment:
        tags["environment"] = environment
    if release := event.release:
        tags["release"] = release
    if server_name := event.server_name:
        tags["server_name"] = server_name

    # Exclude None values
    return {key: value for key, value in tags.items() if value}


def process_issue_events(ingest_events: list[InterchangeIssueEvent]):
    """
    Accepts a list of events to ingest. Events should be:
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
        event.contexts = generate_contexts(event)
        event_tags = generate_tags(event)
        title = ""
        culprit = ""
        metadata: dict[str, Any] = {}
        if event.type in [IssueEventType.ERROR, IssueEventType.DEFAULT]:
            sentry_event = ErrorEvent()
            metadata = sentry_event.get_metadata(event.dict())
            if event.type == IssueEventType.ERROR and metadata:
                full_title = sentry_event.get_title(metadata)
            else:
                message = event.message if event.message else event.logentry
                full_title = (
                    transform_parameterized_message(message)
                    if message
                    else "<untitled>"
                )
                culprit = (
                    event.transaction
                    if event.transaction
                    else generate_culprit(event.dict())
                )
            title = truncatechars(full_title)
            culprit = sentry_event.get_location(event.dict())
        elif event.type == IssueEventType.CSP:
            humanized_directive = event.csp.effective_directive.replace("-src", "")
            uri = urlparse(event.csp.blocked_uri).netloc
            full_title = title = f"Blocked '{humanized_directive}' from '{uri}'"
            culprit = "fake culprit"
            event_data["csp"] = event.csp.dict()

        issue_hash = generate_hash(title, culprit, event.type, event.fingerprint)
        if metadata:
            event_data["metadata"] = metadata
        if platform := event.platform:
            event_data["platform"] = platform
        if modules := event.modules:
            event_data["modules"] = modules
        if sdk := event.sdk:
            event_data["sdk"] = sdk.dict(exclude_none=True)
        if request := event.request:
            event_data["request"] = request.dict(exclude_none=True)
        if environment := event.environment:
            event_data["environment"] = environment

        # Message is str
        # Logentry is {"params": etc} Message format
        if logentry := event.logentry:
            event_data["logentry"] = logentry.dict(exclude_none=True)
        elif message := event.message:
            if isinstance(message, str):
                event_data["logentry"] = {"formatted": message}
            else:
                event_data["logentry"] = message.dict(exclude_none=True)
        if message := event.message:
            event_data["message"] = (
                message if isinstance(message, str) else message.formatted
            )
        # When blank, the API will default to the title anyway
        elif title != full_title:
            # If the title is truncated, store the full title
            event_data["message"] = full_title

        if breadcrumbs := event.breadcrumbs:
            event_data["breadcrumbs"] = devalue(breadcrumbs)
        if exception := event.exception:
            event_data["exception"] = devalue(exception)
        if extra := event.extra:
            event_data["extra"] = extra
        if user := event.user:
            event_data["user"] = user.dict(exclude_none=True)
        if contexts := event.contexts:
            event_data["contexts"] = contexts.dict(exclude_none=True)

        processing_events.append(
            ProcessingEvent(
                event=ingest_event,
                issue_hash=issue_hash,
                title=title,
                transaction=culprit,
                metadata=metadata,
                event_data=event_data,
                event_tags=event_tags,
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
            "metadata": processing_event.metadata,
            "first_seen": processing_event.event.received,
            "last_seen": processing_event.event.received,
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
                        project_id=project_id,
                        search_vector=SearchVector(Value(issue_defaults["title"])),
                        **issue_defaults,
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
                type=event_type,
                timestamp=processing_event.event.payload.timestamp,
                received=processing_event.event.received,
                title=processing_event.title,
                transaction=processing_event.transaction,
                data=processing_event.event_data,
                tags=processing_event.event_tags,
            )
        )

    update_issues(processing_events)

    if issues_to_reopen:
        Issue.objects.filter(id__in=issues_to_reopen).update(
            status=EventStatus.UNRESOLVED
        )
        Notification.objects.filter(issues__in=issues_to_reopen).delete()

    # ignore_conflicts because we could have an invalid duplicate event_id
    IssueEvent.objects.bulk_create(issue_events, ignore_conflicts=True)
