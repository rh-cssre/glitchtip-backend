import os
from collections import defaultdict
from datetime import datetime, timedelta
from operator import itemgetter
from typing import Any, Literal
from urllib.parse import ParseResult, urlparse

from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import connection, transaction
from django.db.models import (
    Exists,
    F,
    OuterRef,
    Q,
    QuerySet,
    Value,
)
from django.db.models.functions import Coalesce, Greatest
from django.db.utils import IntegrityError
from django.utils import timezone
from django_redis import get_redis_connection
from ninja import Schema
from user_agents import parse

from apps.alerts.constants import ISSUE_IDS_KEY
from apps.alerts.models import Notification
from apps.difs.models import DebugInformationFile
from apps.difs.tasks import event_difs_resolve_stacktrace
from apps.environments.models import Environment, EnvironmentProject
from apps.issue_events.constants import MAX_TAG_LENGTH, EventStatus, LogLevel
from apps.issue_events.models import (
    Issue,
    IssueEvent,
    IssueEventType,
    IssueHash,
    TagKey,
    TagValue,
)
from apps.performance.models import (
    TransactionEvent,
    TransactionGroup,
    TransactionGroupAggregate,
)
from apps.projects.models import Project
from apps.releases.models import Release
from apps.sourcecode.models import DebugSymbolBundle
from sentry.culprit import generate_culprit
from sentry.eventtypes.error import ErrorEvent
from sentry.utils.strings import truncatechars

from ..shared.schema.contexts import (
    BrowserContext,
    Contexts,
    DeviceContext,
    OSContext,
)
from .interfaces import IssueStats, IssueUpdate, ProcessingEvent
from .javascript_event_processor import JavascriptEventProcessor
from .model_functions import PGAppendAndLimitTsVector
from .schema import (
    ErrorIssueEventSchema,
    EventException,
    IngestIssueEvent,
    InterchangeTransactionEvent,
    IssueEventSchema,
    IssueTaskMessage,
    SourceMapImage,
    ValueEventException,
)
from .utils import generate_hash, remove_bad_chars, transform_parameterized_message


def _truncate_string(s: str | None, max_len: int) -> str:
    """Safely truncates a string if it's not None."""
    if not s:
        return ""
    return s[:max_len]


# Search settings
MAX_SEARCH_PART_LENGTH = 250
MAX_FILENAME_LEN = 100
MAX_TOTAL_FILENAMES = 5
MAX_FRAMES_PER_STACKTRACE = 3
MAX_STACKTRACES_TO_PROCESS = 2
MAX_VECTOR_STRING_SEGMENT_LEN = 2048  # 2KB

STATS_TABLE_CONFIG = {
    "projects_issueeventprojecthourlystatistic": {"id_column": "project_id"},
    "projects_transactioneventprojecthourlystatistic": {"id_column": "project_id"},
    "issue_events_issueaggregate": {"id_column": "issue_id"},
}

StatsTableName = Literal[
    "projects_issueeventprojecthourlystatistic",
    "projects_transactioneventprojecthourlystatistic",
    "issue_events_issueaggregate",
]


def _get_or_create_related_models(
    release_set: set,
    environment_set: set,
    project_set: set,
) -> tuple[list[tuple[str, int, int]], QuerySet]:
    """
    Given sets of release, environment, and project data,
    creates them if they don't exist, and returns release data and project data.
    """
    release_version_set = {version for version, _, _ in release_set}
    environment_name_set = {name for name, _, _ in environment_set}

    projects_query = Project.objects.filter(id__in=project_set)
    annotations = {
        "release_id": Coalesce("releases__id", Value(None)),
        "release_name": Coalesce("releases__version", Value(None)),
        "environment_id": Coalesce("environment__id", Value(None)),
        "environment_name": Coalesce("environment__name", Value(None)),
    }
    values_list = [
        "id",
        "release_id",
        "release_name",
        "environment_id",
        "environment_name",
    ]

    projects_with_data = (
        projects_query.annotate(**annotations)
        .filter(release_name__in=release_version_set.union({None}))
        .filter(environment_name__in=environment_name_set.union({None}))
        .values(*values_list)
    )

    releases = get_and_create_releases(release_set, projects_with_data)
    create_environments(environment_set, projects_with_data)

    return releases, projects_with_data


def get_search_vector(event: ProcessingEvent) -> str:
    """
    Get string for postgres search vector. The string must be short to ensure
    performance.
    """
    parts: set[str] = set()

    if title := event.title:
        parts.add(_truncate_string(title, MAX_SEARCH_PART_LENGTH))
    if transaction := event.transaction:
        parts.add(_truncate_string(transaction, MAX_SEARCH_PART_LENGTH))

    payload = event.payload
    if request := payload.request:
        # Simplify URL to keep concise
        if url := request.url:
            try:
                parsed_url: ParseResult = urlparse(url)
                truncated_path = _truncate_string(
                    parsed_url.path, MAX_SEARCH_PART_LENGTH
                )
                scheme_netloc = ""
                if parsed_url.scheme and parsed_url.netloc:
                    scheme_netloc = f"{parsed_url.scheme}://{parsed_url.netloc}"
                elif parsed_url.netloc:  # Fallback
                    scheme_netloc = parsed_url.netloc
                if scheme_netloc or truncated_path:  # Only add if we have something
                    simplified_url = f"{scheme_netloc}{truncated_path}"
                    parts.add(_truncate_string(simplified_url, MAX_SEARCH_PART_LENGTH))
            except ValueError:
                parts.add(_truncate_string(url, MAX_SEARCH_PART_LENGTH))

    # Add stacktrace filenames
    filenames_to_add: list[str] = []
    exception_values_list: list[EventException] | None = None
    if (
        isinstance(payload, ErrorIssueEventSchema)
        and payload.exception
        and isinstance(payload.exception, ValueEventException)
    ):
        exception_values_list = payload.exception.values

    if exception_values_list:
        processed_stacktraces_count = 0
        for exc_data in exception_values_list:
            if processed_stacktraces_count >= MAX_STACKTRACES_TO_PROCESS:
                break
            if not exc_data.stacktrace:
                continue
            frames_list = exc_data.stacktrace.frames
            frames_from_this_stacktrace = 0
            for frame in reversed(frames_list):
                if frames_from_this_stacktrace >= MAX_FRAMES_PER_STACKTRACE:
                    break
                filename_val = frame.filename
                if frame.filename:
                    basename = _truncate_string(
                        os.path.basename(str(filename_val)), MAX_FILENAME_LEN
                    )
                    if basename:
                        filenames_to_add.append(basename)
                        frames_from_this_stacktrace += 1

            if frames_from_this_stacktrace > 0:
                processed_stacktraces_count += 1

    for fname in filenames_to_add[:MAX_TOTAL_FILENAMES]:
        parts.add(fname)

    final_vector_string_parts = sorted([p for p in parts if p])
    final_vector_string = " ".join(final_vector_string_parts)

    if len(final_vector_string) > MAX_VECTOR_STRING_SEGMENT_LEN:
        # Try to cut at a space to avoid breaking words mid-lexeme
        limit_idx = final_vector_string.rfind(" ", 0, MAX_VECTOR_STRING_SEGMENT_LEN)
        if limit_idx == -1:  # No space found, hard truncate
            final_vector_string = final_vector_string[:MAX_VECTOR_STRING_SEGMENT_LEN]
        else:
            final_vector_string = final_vector_string[:limit_idx]

    return remove_bad_chars(final_vector_string)


def update_issues(processing_events: list[ProcessingEvent]):
    """
    Update any existing issues based on new statistics
    """
    issues_to_update: dict[int, IssueUpdate] = {}
    for processing_event in processing_events:
        issue_id = processing_event.issue_id
        if processing_event.issue_created or not issue_id:
            continue

        vector = get_search_vector(processing_event)
        if issue_id in issues_to_update:
            issues_to_update[issue_id].added_count += 1
            issues_to_update[issue_id].search_vector += f" {vector}"
            if issues_to_update[issue_id].last_seen < processing_event.received:
                issues_to_update[issue_id].last_seen = processing_event.received
        else:
            issues_to_update[issue_id] = IssueUpdate(
                last_seen=processing_event.received,
                search_vector=vector,
            )

    for issue_id, value in issues_to_update.items():
        Issue.objects.filter(id=issue_id).update(
            count=F("count") + value.added_count,
            search_vector=PGAppendAndLimitTsVector(
                F("search_vector"),
                Value(value.search_vector),
                Value(settings.SEARCH_MAX_LEXEMES),
                Value("english"),
            ),
            last_seen=Greatest(F("last_seen"), value.last_seen),
        )


def generate_contexts(event: IngestIssueEvent) -> Contexts:
    """
    Add additional contexts if they aren't already set
    """
    contexts = event.contexts if event.contexts else Contexts({})

    if request := event.request:
        if isinstance(request.headers, list):
            if ua_string := next(
                (x[1] for x in request.headers if x[0] == "User-Agent"), None
            ):
                user_agent = parse(ua_string)
                if "browser" not in contexts:
                    contexts["browser"] = BrowserContext(
                        name=user_agent.browser.family,
                        version=user_agent.browser.version_string,
                    )
                if "os" not in contexts:
                    contexts["os"] = OSContext(
                        name=user_agent.os.family, version=user_agent.os.version_string
                    )
                if "device" not in contexts:
                    device = user_agent.device
                    contexts["device"] = DeviceContext(
                        family=device.family,
                        model=device.model,
                        brand=device.brand,
                    )
    return contexts


def generate_tags(event: IngestIssueEvent) -> dict[str, str]:
    """Generate key-value tags based on context and other event data"""
    tags: dict[str, str | None] = event.tags if isinstance(event.tags, dict) else {}

    if contexts := event.contexts:
        if browser := contexts.get("browser"):
            if isinstance(browser, BrowserContext):
                tags["browser.name"] = browser.name
                tags["browser"] = f"{browser.name} {browser.version}"
        if os := contexts.get("os"):
            if isinstance(os, OSContext):
                tags["os.name"] = os.name
        if device := contexts.get("device"):
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
            and event.project_id == project_id
            and event.issue_hash == issue_hash
        ):
            event.issue_id = issue_id


def create_environments(
    environment_set: set[tuple[str, int, int]], projects_with_data: QuerySet
):
    """
    Create newly seen environments.
    Functions determines which, if any, environments are present in event data
    but not the database. Optimized to do a much work in python and reduce queries.
    """
    environments_to_create = [
        Environment(name=name, organization_id=organization_id)
        for name, project_id, organization_id in environment_set
        if not next(
            (
                x
                for x in projects_with_data
                if x["environment_name"] == name and x["id"] == project_id
            ),
            None,
        )
    ]

    if environments_to_create:
        Environment.objects.bulk_create(environments_to_create, ignore_conflicts=True)
        query = Q()
        for environment in environments_to_create:
            query |= Q(
                name=environment.name, organization_id=environment.organization_id
            )
        environments = Environment.objects.filter(query)
        environment_projects: list = []
        for environment in environments:
            project_id = next(
                project_id
                for (name, project_id, organization_id) in environment_set
                if environment.name == name
                and environment.organization_id == organization_id
            )
            environment_projects.append(
                EnvironmentProject(project_id=project_id, environment=environment)
            )
        EnvironmentProject.objects.bulk_create(
            environment_projects, ignore_conflicts=True
        )


def get_and_create_releases(
    release_set: set[tuple[str, int, int]], projects_with_data: QuerySet
) -> list[tuple[str, int, int]]:
    """
    Create newly seen releases.
    functions determines which, if any, releases are present in event data
    but not the database. Optimized to do a much work in python and reduce queries.
    Return list of tuples: Release version, project_id, release_id
    """
    releases_to_create = [
        Release(version=release_name, organization_id=organization_id)
        for release_name, project_id, organization_id in release_set
        if not next(
            (
                x
                for x in projects_with_data
                if x["release_name"] == release_name and x["id"] == project_id
            ),
            None,
        )
    ]
    releases: list | QuerySet = []
    if releases_to_create:
        # Create database records for any release that doesn't exist
        Release.objects.bulk_create(releases_to_create, ignore_conflicts=True)
        query = Q()
        for release in releases_to_create:
            query |= Q(version=release.version, organization_id=release.organization_id)
        releases = Release.objects.filter(query)
        ReleaseProject = Release.projects.through
        release_projects = [
            ReleaseProject(
                release=release,
                project_id=next(
                    project_id
                    for (version, project_id, organization_id) in release_set
                    if release.version == version
                    and release.organization_id == organization_id
                ),
            )
            for release in releases
        ]
        ReleaseProject.objects.bulk_create(release_projects, ignore_conflicts=True)
    return [
        (
            version,
            project_id,
            next(
                (
                    project["release_id"]
                    for project in projects_with_data
                    if project["release_name"] == version
                    and project["id"] == project_id
                ),
                next(
                    (
                        release.id
                        for release in releases
                        if release.version == version
                        and release.organization_id == organization_id
                    ),
                    0,
                ),
            ),
        )
        for version, project_id, organization_id in release_set
    ]


def process_issue_events(messages: list[IssueTaskMessage]):
    """
    Accepts a list of events to ingest. Events should be:
    - Few enough to save in a single DB call
    - Permission is already checked, these events are to write to the DB
    - Some invalid events are tolerated (ignored), including duplicate event id

    When there is an error in this function, care should be taken as to when to log,
    error, or ignore. If the SDK sends "weird" data, we want to log that.
    It's better to save a minimal event than to ignore it.
    """

    # Fetch any needed releases, environments, and whether there is a dif file association
    # Get unique release/environment for each project_id
    release_set = {
        (event.payload.release, event.project_id, event.organization_id)
        for event in messages
        if event.payload.release
    }
    environment_set = {
        (event.payload.environment[:255], event.project_id, event.organization_id)
        for event in messages
        if event.payload.environment
    }
    project_set = {project_id for _, project_id, _ in release_set}.union(
        {project_id for _, project_id, _ in environment_set}
    )
    release_version_set = {version for version, _, _ in release_set}

    releases, projects_with_data = _get_or_create_related_models(
        release_set, environment_set, project_set
    )

    projects_with_data = projects_with_data.annotate(
        has_difs=Exists(DebugInformationFile.objects.filter(project_id=OuterRef("pk")))
    )

    sourcemap_images = [
        image
        for event in messages
        if isinstance(event.payload, ErrorIssueEventSchema) and event.payload.debug_meta
        for image in event.payload.debug_meta.images
        if isinstance(image, SourceMapImage)
    ]

    # Get each unique filename from each stacktrace frame
    # The nesting is from the variable ways ingest data is accepted
    # IMO it's even harder to read unnested...
    filename_set = {
        frame.filename.split("/")[-1]
        for event in messages
        if isinstance(event.payload, (ErrorIssueEventSchema, IssueEventSchema))
        and event.payload.exception
        for exception in (
            event.payload.exception
            if isinstance(event.payload.exception, list)
            else event.payload.exception.values
        )
        if exception.stacktrace
        for frame in exception.stacktrace.frames
        if frame.filename
    }

    debug_files = (
        DebugSymbolBundle.objects.filter(
            organization__in={event.organization_id for event in messages}
        )
        .filter(
            Q(
                release__version__in=release_version_set,
                release__projects__in=project_set,
                file__name__in=filename_set,
            )
            | Q(debug_id__in={image.debug_id for image in sourcemap_images})
        )
        .select_related("file", "sourcemap_file", "release")
    )
    now = timezone.now()
    # Update last used if older than 1 day, to minimize queries
    if debug_files:
        update_threshold = now - timedelta(days=1)
        ids_to_update = list(
            debug_files.filter(last_used__lt=update_threshold).values_list(
                "pk", flat=True
            )
        )
        DebugSymbolBundle.objects.filter(pk__in=ids_to_update).select_for_update(
            skip_locked=True
        ).update(last_used=now)

    # Collected/calculated event data while processing
    processing_events: list[ProcessingEvent] = []
    # Collect Q objects for bulk issue hash lookup
    q_objects = Q()
    for ingest_event in messages:
        event = ingest_event.payload
        event.contexts = generate_contexts(event)
        event_tags = generate_tags(event)
        title = ""
        culprit = ""
        metadata: dict[str, Any] = {}

        release_id = next(
            (
                release_id
                for version, project_id, release_id in releases
                if version == event_tags.get("release")
                and ingest_event.project_id == project_id
            ),
            None,
        )
        if event.platform in ("javascript", "node"):
            event_debug_files = [
                debug_file
                for debug_file in debug_files
                if debug_file.organization_id == ingest_event.organization_id
            ]

            # Assign code_file to file headers
            if event.debug_meta:
                for sourcemap_image in [
                    image
                    for image in event.debug_meta.images
                    if isinstance(image, SourceMapImage)
                ]:
                    for debug_file in event_debug_files:
                        if sourcemap_image.debug_id == debug_file.debug_id:
                            debug_file.data["code_file"] = sourcemap_image.code_file

            JavascriptEventProcessor(
                release_id,
                event,
                [
                    debug_file
                    for debug_file in event_debug_files
                    if debug_file.release_id == release_id
                    or debug_file.data.get("code_file")
                ],
            ).transform()
        elif (
            isinstance(event, ErrorIssueEventSchema)
            and event.exception
            and next(
                (
                    project["has_difs"]
                    for project in projects_with_data
                    if project["id"] == ingest_event.project_id
                ),
                False,
            )
        ):
            event_difs_resolve_stacktrace(event, ingest_event.project_id)

        event_data = event.model_dump(
            mode="json",
            include={
                "platform",
                "modules",
                "sdk",
                "request",
                "environment",
                "extra",
                "user",
                "exception",
                "breadcrumbs",
            },
            exclude_none=True,
            exclude_defaults=True,
        )
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

        if contexts := event.contexts:
            # Contexts may contain dict or Schema
            event_data["contexts"] = {
                key: value.dict(exclude_none=True)
                if isinstance(value, Schema)
                else value
                for key, value in contexts.items()
            }

        processing_events.append(
            ProcessingEvent(
                project_id=ingest_event.project_id,
                organization_id=ingest_event.organization_id,
                received=ingest_event.received,
                payload=ingest_event.payload,
                issue_hash=issue_hash,
                title=title,
                level=LogLevel.from_string(event.level) if event.level else None,
                transaction=culprit,
                metadata=metadata,
                event_data=event_data,
                event_tags=event_tags,
                release_id=release_id,
            )
        )
        q_objects |= Q(project_id=ingest_event.project_id, value=issue_hash)

    hash_queryset = IssueHash.objects.filter(q_objects).values(
        "value", "project_id", "issue_id", "issue__status"
    )
    issue_events: list[IssueEvent] = []
    issues_to_reopen = []
    # Group events by time and project for event count statistics
    data_stats: defaultdict[datetime, defaultdict[int, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    issue_hourly_stats: defaultdict[datetime, defaultdict[int, IssueStats]] = (
        defaultdict(lambda: defaultdict(lambda: {"count": 0, "organization_id": None}))
    )

    for processing_event in processing_events:
        event_type = processing_event.payload.type
        project_id = processing_event.project_id
        issue_defaults = {
            "type": event_type,
            "title": remove_bad_chars(processing_event.title),
            "metadata": remove_bad_chars(processing_event.metadata),
            "first_seen": processing_event.received,
            "last_seen": processing_event.received,
        }
        if level := processing_event.level:
            issue_defaults["level"] = level
        for hash_obj in hash_queryset:
            if (
                hash_obj["value"].hex == processing_event.issue_hash
                and hash_obj["project_id"] == project_id
            ):
                processing_event.issue_id = hash_obj["issue_id"]
                if hash_obj["issue__status"] == EventStatus.RESOLVED:
                    issues_to_reopen.append(hash_obj["issue_id"])
                break

        if not processing_event.issue_id:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects_projectcounter (project_id, value)
                    VALUES (%s, 1)
                    ON CONFLICT (project_id) DO UPDATE
                    SET value = projects_projectcounter.value + 1
                    RETURNING value;
                    """,
                    [project_id],
                )
                issue_defaults["short_id"] = cursor.fetchone()[0]
            try:
                with transaction.atomic():
                    issue = Issue.objects.create(
                        project_id=project_id,
                        search_vector=SearchVector(
                            Value(get_search_vector(processing_event))
                        ),
                        **issue_defaults,
                    )
                    new_issue_hash = IssueHash.objects.create(
                        issue=issue,
                        value=processing_event.issue_hash,
                        project_id=project_id,
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
                    project_id=project_id, value=processing_event.issue_hash
                ).issue_id

        hour_received = processing_event.received.replace(
            minute=0, second=0, microsecond=0
        )
        data_stats[hour_received][processing_event.project_id] += 1
        if processing_event.issue_id:  # Only count if issue is known
            issue_hourly_stats[hour_received][processing_event.issue_id]["count"] += 1
            issue_hourly_stats[hour_received][processing_event.issue_id][
                "organization_id"
            ] = processing_event.organization_id

        issue_events.append(
            IssueEvent(
                id=processing_event.payload.event_id,
                issue_id=processing_event.issue_id,
                type=event_type,
                level=processing_event.level
                if processing_event.level
                else LogLevel.ERROR,
                timestamp=processing_event.payload.timestamp,
                received=processing_event.received,
                title=remove_bad_chars(processing_event.title),
                transaction=processing_event.transaction,
                data=remove_bad_chars(processing_event.event_data),
                hashes=[processing_event.issue_hash],
                tags=processing_event.event_tags,
                release_id=processing_event.release_id,
            )
        )

    update_issues(processing_events)

    if settings.CACHE_IS_REDIS:
        # Add set of issue_ids for alerts to process later
        with get_redis_connection("default") as con:
            if (
                con.sadd(
                    ISSUE_IDS_KEY, *{event.issue_id for event in processing_events}
                )
                > 0
            ):
                # Set a long expiration time when a key is added
                # We want all keys to have a long "sanity check" TTL to avoid redis out
                # of memory errors (we can't ensure end users use all keys lru eviction)
                con.expire(ISSUE_IDS_KEY, 3600)

    if issues_to_reopen:
        Issue.objects.filter(id__in=issues_to_reopen).update(
            status=EventStatus.UNRESOLVED
        )
        Notification.objects.filter(issues__in=issues_to_reopen).delete()

    # ignore_conflicts because we could have an invalid duplicate event_id, received
    IssueEvent.objects.bulk_create(issue_events, ignore_conflicts=True)

    update_tags(processing_events)
    update_statistics(
        data_stats,
        table_name="projects_issueeventprojecthourlystatistic",
    )
    update_org_statistics(
        issue_hourly_stats,
        table_name="issue_events_issueaggregate",
    )


def update_statistics(
    stats_data: defaultdict[datetime, defaultdict[int, int]],
    table_name: StatsTableName,
):
    """
    Generic function to bulk upsert hourly statistics.
    """
    # Runtime check for security
    if table_name not in STATS_TABLE_CONFIG:
        raise ValueError(f"Invalid table_name for statistics update: {table_name}")

    id_column_name = STATS_TABLE_CONFIG[table_name]["id_column"]

    data = sorted(
        [
            [date, key, value]
            for date, inner_dict in stats_data.items()
            for key, value in inner_dict.items()
        ],
        key=itemgetter(0, 1),
    )

    if not data:
        return

    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s)", x) for x in data)
        sql = (
            f"INSERT INTO {table_name} (date, {id_column_name}, count)\n"
            f"VALUES {args_str}\n"
            f"ON CONFLICT ({id_column_name}, date)\n"
            f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count;"
        )
        cursor.execute(sql)


def update_org_statistics(
    stats_data: defaultdict[datetime, defaultdict[int, IssueStats]],
    table_name: StatsTableName,
):
    """
    Bulk upserts hourly statistics for the XAggregate model.

    This function is specifically designed to handle the data structure that
    includes organization_id, for use with the new composite primary key on
    the issues_issueaggregate table.
    """
    id_column_name = STATS_TABLE_CONFIG[table_name]["id_column"]
    data = []

    for date, inner_dict in stats_data.items():
        for issue_id, stats_dict in inner_dict.items():
            # Only include entries where the org_id was successfully set
            if (organization_id := stats_dict.get("organization_id")) is not None:
                data.append([date, organization_id, issue_id, stats_dict["count"]])

    if not data:
        return

    # Sort by all key components to avoid deadlocks on concurrent writes
    data.sort(key=itemgetter(0, 1, 2))

    with connection.cursor() as cursor:
        # Prepare the data for a single, bulk INSERT statement
        args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s)", x) for x in data)

        # The ON CONFLICT target must match the composite primary key
        # of (organization_id, issue_id, date)
        conflict_target = f"(organization_id, {id_column_name}, date)"

        # Construct the final SQL query
        sql = (
            f"INSERT INTO {table_name} (date, organization_id, {id_column_name}, count)\n"
            f"VALUES {args_str}\n"
            f"ON CONFLICT {conflict_target}\n"
            f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count;"
        )
        cursor.execute(sql)


def update_transaction_group_stats(
    stats_data: defaultdict[datetime, defaultdict[int, dict]],
):
    """
    Bulk upserts 1-minute statistics for the TransactionGroupAggregate model.
    """
    table_name = TransactionGroupAggregate._meta.db_table
    data = []

    for date, inner_dict in stats_data.items():
        for group_id, stats in inner_dict.items():
            # Ensure organization_id is present before appending
            if (organization_id := stats.get("organization_id")) is not None:
                data.append(
                    (
                        date,
                        organization_id,
                        group_id,
                        stats["count"],
                        stats["total_duration"],
                        stats["sum_of_squares_duration"],
                        "{}",
                    )
                )

    if not data:
        return

    # Sort by the primary key to avoid potential deadlocks on concurrent writes
    data.sort(key=itemgetter(0, 1, 2))

    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s,%s,%s)", x) for x in data)

        # The ON CONFLICT target must match the composite PK
        conflict_target = "(date, organization_id, group_id)"

        # Construct the final SQL query for an atomic "upsert"
        sql = f"""
            INSERT INTO {table_name} (
                date, organization_id, group_id, count,
                total_duration, sum_of_squares_duration, histogram
            )
            VALUES {args_str}
            ON CONFLICT {conflict_target}
            DO UPDATE SET
                count = {table_name}.count + EXCLUDED.count,
                total_duration = {table_name}.total_duration + EXCLUDED.total_duration,
                sum_of_squares_duration = {table_name}.sum_of_squares_duration + EXCLUDED.sum_of_squares_duration;
        """
        cursor.execute(sql)


TagStats = defaultdict[
    datetime,
    defaultdict[int, defaultdict[int, defaultdict[int, int]]],
]


def update_tags(processing_events: list[ProcessingEvent]):
    # Truncate long values
    for processing_event in processing_events:
        processing_event.event_tags = {
            str(key)[:MAX_TAG_LENGTH]: str(value)[:MAX_TAG_LENGTH]
            for key, value in processing_event.event_tags.items()
        }
    keys = sorted({key for d in processing_events for key in d.event_tags.keys()})
    values = sorted(
        {value for d in processing_events for value in d.event_tags.values()}
    )

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
        minute_received = processing_event.received.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for key, value in processing_event.event_tags.items():
            key_id = tag_keys[key]
            value_id = tag_values[value]
            tag_stats[minute_received][processing_event.issue_id][key_id][value_id] += 1

    if not tag_stats:
        return

    # Sort to mitigate deadlocks
    data = sorted(
        [
            [date, issue_id, key_id, value_id, count]
            for date, d1 in tag_stats.items()
            for issue_id, d2 in d1.items()
            for key_id, d3 in d2.items()
            for value_id, count in d3.items()
        ],
        key=itemgetter(0, 1, 2, 3),
    )
    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s)", x) for x in data)
        sql = (
            "INSERT INTO issue_events_issuetag (date, issue_id, tag_key_id, tag_value_id, count)\n"
            f"VALUES {args_str}\n"
            "ON CONFLICT (issue_id, date, tag_key_id, tag_value_id)\n"
            "DO UPDATE SET count = issue_events_issuetag.count + EXCLUDED.count;"
        )
        cursor.execute(sql)


# Transactions
def process_transaction_events(ingest_events: list[InterchangeTransactionEvent]):
    release_set = {
        (event.payload.release, event.project_id, event.organization_id)
        for event in ingest_events
        if event.payload.release
    }
    environment_set = {
        (event.payload.environment[:255], event.project_id, event.organization_id)
        for event in ingest_events
        if event.payload.environment
    }
    project_set = {project_id for _, project_id, _ in release_set}.union(
        {project_id for _, project_id, _ in environment_set}
    )
    _get_or_create_related_models(release_set, environment_set, project_set)
    transactions = []

    for ingest_event in ingest_events:
        event = ingest_event.payload
        contexts = event.contexts
        request = event.request
        trace_id = contexts["trace"]["trace_id"]
        op = ""
        if isinstance(contexts, dict):
            trace = contexts.get("trace", {})
            if isinstance(trace, dict):
                op = str(trace.get("op", ""))
        method = ""
        if request and request.method:
            method = request.method

        # TODO tags

        group, group_created = TransactionGroup.objects.get_or_create(
            project_id=ingest_event.project_id,
            transaction=event.transaction[:1024],  # Truncate
            op=op,
            method=method,
        )

        transactions.append(
            TransactionEvent(
                group=group,
                organization_id=ingest_event.organization_id,
                data=remove_bad_chars(
                    {
                        "request": request.dict() if request else None,
                        "sdk": event.sdk.dict() if event.sdk else None,
                        "platform": event.platform,
                    }
                ),
                trace_id=trace_id,
                event_id=event.event_id,
                timestamp=event.timestamp,
                start_timestamp=event.start_timestamp,
            )
        )
    TransactionEvent.objects.bulk_create(transactions, ignore_conflicts=True)

    group_stats: defaultdict[datetime, defaultdict[int, dict]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "count": 0,
                "total_duration": 0.0,
                "sum_of_squares_duration": 0.0,
            }
        )
    )
    for perf_transaction in transactions:
        # Truncate the timestamp to the minute for our 1-minute aggregation buckets.
        minute_timestamp = perf_transaction.start_timestamp.replace(
            second=0, microsecond=0
        )
        group_id = perf_transaction.group_id
        duration: int | None = perf_transaction.duration_ms

        stats_bucket = group_stats[minute_timestamp][group_id]

        # Set organization_id once per bucket, as it's part of the key.
        if "organization_id" not in stats_bucket:
            stats_bucket["organization_id"] = perf_transaction.organization_id

        stats_bucket["count"] += 1
        if duration:
            stats_bucket["total_duration"] += duration
            stats_bucket["sum_of_squares_duration"] += duration**2
    update_transaction_group_stats(group_stats)

    data_stats: defaultdict[datetime, defaultdict[int, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for perf_transaction in transactions:
        hour_received = perf_transaction.start_timestamp.replace(
            minute=0, second=0, microsecond=0
        )
        data_stats[hour_received][perf_transaction.group.project_id] += 1
    update_statistics(
        data_stats,
        table_name="projects_transactioneventprojecthourlystatistic",
    )
