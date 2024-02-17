from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Union
from urllib.parse import urlparse

from django.contrib.postgres.search import SearchVector
from django.db import connection, transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.db.utils import IntegrityError
from ninja import Schema
from user_agents import parse

from alerts.models import Notification
from apps.issue_events.constants import EventStatus
from apps.issue_events.models import (
    Issue,
    IssueEvent,
    IssueEventType,
    IssueHash,
    TagKey,
    TagValue,
)
from apps.environments.models import Environment, EnvironmentProject
from apps.releases.models import Release
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


def check_set_issue_id(
    processing_events: list[ProcessingEvent],
    project_id: int,
    issue_hash: str,
    issue_id: int,
):
    """
    It's common to receive two duplicate events at the same time,
    where the issue has never been seen before. This is an optimization
    that checks if there is a known project/hash. If so, we can infer the
    issue_id.
    """
    for event in processing_events:
        if (
            event.issue_id is None
            and event.event.project_id == project_id
            and event.issue_hash == issue_hash
        ):
            event.issue_id = issue_id


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
    releases_set = {
        (event.payload.release, event.project_id, event.organization_id)
        for event in ingest_events
        if event.payload.release
    }
    Release.objects.bulk_create(
        [
            Release(version=version, organization_id=organization_id)
            for (version, _, organization_id) in releases_set
        ],
        ignore_conflicts=True,
    )
    releases = Release.objects.filter(
        version__in={version for (version, _, _) in releases_set},
        organization_id__in={
            organization_id for (_, _, organization_id) in releases_set
        },
    )
    ReleaseProject = Release.projects.through
    release_projects: list = []
    for release in releases:
        project_id = next(
            project_id
            for (version, project_id, organization_id) in releases_set
            if release.version == version and release.organization_id == organization_id
        )
        release_projects.append(ReleaseProject(project_id=project_id, release=release))
    ReleaseProject.objects.bulk_create(release_projects, ignore_conflicts=True)

    environments_set = {
        (event.payload.environment[:256], event.project_id, event.organization_id)
        for event in ingest_events
        if event.payload.environment
    }
    Environment.objects.bulk_create(
        [
            Environment(name=name, organization_id=organization_id)
            for (name, _, organization_id) in environments_set
        ],
        ignore_conflicts=True,
    )
    environments = Environment.objects.filter(
        name__in={name for (name, _, _) in environments_set},
        organization_id__in={
            organization_id for (_, _, organization_id) in environments_set
        },
    )
    environment_projects: list = []
    for environment in environments:
        project_id = next(
            project_id
            for (name, project_id, organization_id) in environments_set
            if environment.name == name
            and environment.organization_id == organization_id
        )
        environment_projects.append(
            EnvironmentProject(project_id=project_id, environment=environment)
        )
    EnvironmentProject.objects.bulk_create(environment_projects, ignore_conflicts=True)

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
            culprit = event.csp.effective_directive
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
                    new_issue_hash = IssueHash.objects.create(
                        issue=issue, value=issue_hash, project_id=project_id
                    )
                    check_set_issue_id(
                        processing_events,
                        issue.project_id,
                        new_issue_hash.value,
                        issue.id,
                    )
                processing_event.issue_id = issue.id
                processing_event.issue_created = True
            except IntegrityError:
                processing_event.issue_id = IssueHash.objects.get(
                    project_id=project_id, value=issue_hash
                ).issue_id
        release_id = next(
            (
                release.id
                for release in releases
                if release.version == processing_event.event_tags.get("release")
            ),
            None,
        )
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
                release_id=release_id,
            )
        )

    update_issues(processing_events)

    if issues_to_reopen:
        Issue.objects.filter(id__in=issues_to_reopen).update(
            status=EventStatus.UNRESOLVED
        )
        Notification.objects.filter(issues__in=issues_to_reopen).delete()

    # ignore_conflicts because we could have an invalid duplicate event_id, received
    IssueEvent.objects.bulk_create(issue_events, ignore_conflicts=True)

    # for processing_event in processing_events:
    #     JavascriptEventProcessor(project.release_id, data).run()

    # Group events by time and project for event count statistics
    data_stats: defaultdict[datetime, defaultdict[int, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for processing_event in processing_events:
        hour_received = processing_event.event.received.replace(
            minute=0, second=0, microsecond=0
        )
        data_stats[hour_received][processing_event.event.project_id] += 1

    update_tags(processing_events)
    update_statistics(data_stats)


def update_statistics(
    project_event_stats: defaultdict[datetime, defaultdict[int, int]],
):
    # Flatten data for a sql param friendly format
    data = [
        [year, key, value]
        for year, inner_dict in project_event_stats.items()
        for key, value in inner_dict.items()
    ]
    # Django ORM cannot support F functions in a bulk_update
    # psycopg3 does not support execute_values
    # https://github.com/psycopg/psycopg/issues/114
    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s)", x) for x in data)
        sql = (
            "INSERT INTO projects_eventprojecthourlystatistic (date, project_id, count)\n"
            f"VALUES {args_str}\n"
            "ON CONFLICT (project_id, date)\n"
            "DO UPDATE SET count = projects_eventprojecthourlystatistic.count + EXCLUDED.count;"
        )
        cursor.execute(sql)


TagStats = defaultdict[
    datetime,
    defaultdict[int, defaultdict[int, defaultdict[int, int]]],
]


def update_tags(processing_events: list[ProcessingEvent]):
    keys = {key for d in processing_events for key in d.event_tags.keys()}
    values = {value for d in processing_events for value in d.event_tags.values()}

    TagKey.objects.bulk_create([TagKey(key=key) for key in keys], ignore_conflicts=True)
    TagValue.objects.bulk_create(
        [TagValue(value=value) for value in values], ignore_conflicts=True
    )
    # Postgres cannot return ids with ignore_conflicts
    tag_keys = {
        tag["key"]: tag["id"] for tag in TagKey.objects.filter(key__in=keys).values()
    }
    tag_values = {
        tag["value"]: tag["id"]
        for tag in TagValue.objects.filter(value__in=values).values()
    }

    tag_stats: TagStats = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    )
    for processing_event in processing_events:
        if processing_event.issue_id is None:
            continue
        # Group by day. More granular allows for a better search
        # Less granular yields better tag filter performance
        minute_received = processing_event.event.received.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for key, value in processing_event.event_tags.items():
            key_id = tag_keys[key]
            value_id = tag_values[value]
            tag_stats[minute_received][processing_event.issue_id][key_id][value_id] += 1

    if not tag_stats:
        return

    data = [
        [date, issue_id, key_id, value_id, count]
        for date, d1 in tag_stats.items()
        for issue_id, d2 in d1.items()
        for key_id, d3 in d2.items()
        for value_id, count in d3.items()
    ]
    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s)", x) for x in data)
        sql = (
            "INSERT INTO issue_events_issuetag (date, issue_id, tag_key_id, tag_value_id, count)\n"
            f"VALUES {args_str}\n"
            "ON CONFLICT (issue_id, date, tag_key_id, tag_value_id)\n"
            "DO UPDATE SET count = issue_events_issuetag.count + EXCLUDED.count;"
        )
        cursor.execute(sql)
