import os
from collections import defaultdict
from datetime import datetime, timedelta
from operator import itemgetter
from typing import Any, Literal
from urllib.parse import ParseResult, urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.core.cache import caches
from django.db import connection, connections, transaction
from django.db.models import Q, Value
from django.db.utils import IntegrityError
from django.utils import timezone
from ninja import Schema
from psycopg.types.json import Jsonb
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
from apps.performance.histogram import (
    merge_durations,
    new_histogram,
    percentile_from_histogram,
)
from apps.performance.models import TransactionGroup
from apps.performance.parameterize import parameterize_description
from apps.projects.models import Project
from apps.releases.models import Release
from apps.sourcecode.models import DebugSymbolBundle
from glitchtip.cold_storage import is_duckdb_available
from glitchtip.partition_manager import UUID7Helper
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
from .schema import (
    ErrorIssueEventSchema,
    EventException,
    InterchangeTransactionEvent,
    IssueEventSchema,
    IssueTaskMessage,
    SourceMapImage,
    TaskIssueEvent,
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
MAX_SPANS_PER_TRANSACTION = 1000

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


async def _get_or_create_related_models(
    release_set: set,
    environment_set: set,
    project_set: set,
    read_only_db: str = "default",
) -> tuple[list[tuple[str, int, int]], list[dict]]:
    """
    Given sets of release, environment, and project data,
    creates them if they don't exist, and returns release data and project data.
    """
    release_version_set = {version for version, _, _ in release_set}
    environment_name_set = {name for name, _, _ in environment_set}

    if not project_set or not release_version_set or not environment_name_set:
        projects_with_data: list[dict] = []
    else:

        def _fetch_projects():
            with connections[read_only_db].cursor() as cursor:
                project_ids = list(project_set)
                release_versions = list(release_version_set)
                environment_names = list(environment_name_set)

                cursor.execute(
                    """
                    SELECT
                        p.id,
                        rp.release_id,
                        r.version AS release_name,
                        ep.environment_id,
                        e.name AS environment_name,
                        EXISTS(
                            SELECT 1 FROM difs_debuginformationfile dif
                            WHERE dif.project_id = p.id LIMIT 1
                        ) AS has_difs
                    FROM projects_project p
                    LEFT JOIN releases_release_projects rp ON p.id = rp.project_id
                    LEFT JOIN releases_release r ON rp.release_id = r.id
                    LEFT JOIN environments_environmentproject ep ON p.id = ep.project_id
                    LEFT JOIN environments_environment e ON ep.environment_id = e.id
                    WHERE p.id = ANY(%s)
                      AND r.version = ANY(%s)
                      AND (e.name = ANY(%s) OR e.name IS NULL)
                    """,
                    [project_ids, release_versions, environment_names],
                )
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

        projects_with_data = await sync_to_async(_fetch_projects)()

    releases = await get_and_create_releases(release_set, projects_with_data)
    await create_environments(environment_set, projects_with_data)

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


async def update_issues(processing_events: list[ProcessingEvent]):
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
                if processing_event.release_id:
                    issues_to_update[
                        issue_id
                    ].last_release_id = processing_event.release_id
            elif (
                not issues_to_update[issue_id].last_release_id
                and processing_event.release_id
            ):
                issues_to_update[issue_id].last_release_id = processing_event.release_id
        else:
            issues_to_update[issue_id] = IssueUpdate(
                last_seen=processing_event.received,
                search_vector=vector,
                last_release_id=processing_event.release_id,
            )

    if not issues_to_update:
        return

    data = sorted(
        [
            (
                issue_id,
                value.added_count,
                value.search_vector,
                value.last_seen,
                value.last_release_id,
            )
            for issue_id, value in issues_to_update.items()
        ],
        key=itemgetter(0),
    )

    def _execute():
        with connection.cursor() as cursor:
            args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s)", x) for x in data)
            max_lexemes = settings.SEARCH_MAX_LEXEMES

            sql = (
                "UPDATE issue_events_issue SET "
                "count = issue_events_issue.count + v.added_count, "
                f"search_vector = append_and_limit_tsvector(issue_events_issue.search_vector, v.new_vector, {max_lexemes}, 'english'::regconfig), "
                "last_seen = GREATEST(issue_events_issue.last_seen, v.last_seen), "
                "last_release_id = COALESCE(v.last_release_id::bigint, issue_events_issue.last_release_id) "
                f"FROM (VALUES {args_str}) AS v(id, added_count, new_vector, last_seen, last_release_id) "
                "WHERE issue_events_issue.id = v.id"
            )
            cursor.execute(sql)

    await sync_to_async(_execute)()


def generate_contexts(event: TaskIssueEvent) -> Contexts:
    """
    Add additional contexts if they aren't already set
    """
    contexts = event.contexts if event.contexts else {}

    if request := event.request:
        # Handle both IngestRequest objects and raw dict data from vtasks
        if isinstance(request, dict):
            headers = request.get("headers")
        else:
            # IngestRequest object - access headers attribute
            headers = getattr(request, "headers", None)

        ua_string = None

        # Headers can be dict or list format
        if isinstance(headers, list):
            ua_string = next((x[1] for x in headers if x[0] == "User-Agent"), None)
        elif isinstance(headers, dict):
            ua_string = headers.get("User-Agent")

        if ua_string:
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


def generate_tags(event: TaskIssueEvent) -> dict[str, str]:
    """Generate key-value tags based on context and other event data"""
    tags: dict[str, str | None] = event.tags if isinstance(event.tags, dict) else {}

    if contexts := event.contexts:
        # Assume contexts are already normalized to typed objects
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


async def create_environments(
    environment_set: set[tuple[str, int, int]], projects_with_data: list[dict]
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
        await Environment.objects.abulk_create(
            environments_to_create, ignore_conflicts=True
        )
        env_pairs = [(e.name, e.organization_id) for e in environments_to_create]

        def _fetch_env_ids():
            with connections["default"].cursor() as cursor:
                values_str = ",".join(cursor.mogrify("(%s,%s)", p) for p in env_pairs)
                cursor.execute(
                    "SELECT id, name, organization_id FROM environments_environment "
                    f"WHERE (name, organization_id) IN (VALUES {values_str})"
                )
                return cursor.fetchall()

        env_rows = await sync_to_async(_fetch_env_ids)()
        environment_projects = []
        for env_id, env_name, env_org_id in env_rows:
            pid = next(
                project_id
                for (name, project_id, organization_id) in environment_set
                if env_name == name and env_org_id == organization_id
            )
            environment_projects.append(
                EnvironmentProject(project_id=pid, environment_id=env_id)
            )
        await EnvironmentProject.objects.abulk_create(
            environment_projects, ignore_conflicts=True
        )


async def get_and_create_releases(
    release_set: set[tuple[str, int, int]], projects_with_data: list[dict]
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
    release_rows: list[tuple] = []
    if releases_to_create:
        # Create database records for any release that doesn't exist
        await Release.objects.abulk_create(releases_to_create, ignore_conflicts=True)
        rel_pairs = [(r.version, r.organization_id) for r in releases_to_create]

        def _fetch_release_ids():
            with connections["default"].cursor() as cursor:
                values_str = ",".join(cursor.mogrify("(%s,%s)", p) for p in rel_pairs)
                cursor.execute(
                    "SELECT id, version, organization_id FROM releases_release "
                    f"WHERE (version, organization_id) IN (VALUES {values_str})"
                )
                return cursor.fetchall()

        release_rows = await sync_to_async(_fetch_release_ids)()
        ReleaseProject = Release.projects.through
        release_projects = [
            ReleaseProject(
                release_id=rel_id,
                project_id=next(
                    project_id
                    for (version, project_id, organization_id) in release_set
                    if rel_version == version and rel_org_id == organization_id
                ),
            )
            for rel_id, rel_version, rel_org_id in release_rows
        ]
        await ReleaseProject.objects.abulk_create(
            release_projects, ignore_conflicts=True
        )
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
                        rel_id
                        for rel_id, rel_version, rel_org_id in release_rows
                        if rel_version == version and rel_org_id == organization_id
                    ),
                    0,
                ),
            ),
        )
        for version, project_id, organization_id in release_set
    ]


def hydrate_stacktrace(event: TaskIssueEvent):
    """
    If an exception has no stacktrace, attempt to find one in threads.
    """
    if not event.exception or not event.exception.values:
        return

    if not event.threads or not event.threads.values:
        return

    threads = event.threads.values
    for exception in event.exception.values:
        if exception.stacktrace:
            continue

        # Match Priority 1: thread.id == exception.thread_id
        match = None
        if exception.thread_id is not None:
            match = next(
                (t for t in threads if str(t.id) == str(exception.thread_id)), None
            )

        # Match Priority 2: thread.current is True
        if not match:
            match = next((t for t in threads if t.current), None)

        if match and match.stacktrace:
            if hasattr(match.stacktrace, "model_copy"):
                exception.stacktrace = match.stacktrace.model_copy(deep=True)
            else:
                exception.stacktrace = match.stacktrace.copy(deep=True)


async def _fetch_issue_hashes_raw(
    pairs: list[tuple[int, str]], db_alias: str
) -> dict[tuple[int, str], dict]:
    """Fetch IssueHash rows with issue status via raw SQL VALUES lookup."""
    if not pairs:
        return {}

    def _execute():
        with connections[db_alias].cursor() as cursor:
            values_str = ",".join(
                cursor.mogrify("(%s,%s::uuid)", (pid, h)) for pid, h in pairs
            )
            cursor.execute(
                f"""
                SELECT ih.project_id, ih.value, ih.issue_id,
                       i.status AS issue__status,
                       i.resolved_in_release_id AS issue__resolved_in_release_id
                FROM issue_events_issuehash ih
                INNER JOIN issue_events_issue i ON i.id = ih.issue_id
                WHERE (ih.project_id, ih.value) IN (VALUES {values_str})
                """
            )
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    rows = await sync_to_async(_execute)()
    return {(h["project_id"], h["value"].hex): h for h in rows}


async def _fetch_issue_hashes(
    processing_events: list[ProcessingEvent], db_alias: str
) -> dict[tuple[int, str], dict]:
    """Collect unique (project_id, issue_hash) pairs and fetch via raw SQL."""
    pairs = list(
        {(pe.project_id, pe.issue_hash) for pe in processing_events if pe.issue_hash}
    )
    return await _fetch_issue_hashes_raw(pairs, db_alias)


async def process_issue_events(
    messages: list[IssueTaskMessage], read_only_db: str = "default"
):
    """
    Accepts a list of events to ingest. Events should be:
    - Few enough to save in a single DB call
    - Permission is already checked, these events are to write to the DB
    - Some invalid events are tolerated (ignored), including duplicate event id

    When there is an error in this function, care should be taken as to when to log,
    error, or ignore. If the SDK sends "weird" data, we want to log that.
    It's better to save a minimal event than to ignore it.
    """
    projects_to_update = {msg.project_id for msg in messages if msg.update_first_event}
    if projects_to_update:
        await Project.objects.filter(
            id__in=projects_to_update, first_event__isnull=True
        ).aupdate(first_event=timezone.now())

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

    releases, projects_with_data = await _get_or_create_related_models(
        release_set, environment_set, project_set, read_only_db
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

    debug_files_qs = (
        DebugSymbolBundle.objects.using(read_only_db)
        .filter(organization__in={event.organization_id for event in messages})
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
    # Materialize once — iterated multiple times below
    debug_files = [df async for df in debug_files_qs]

    now = timezone.now()
    # Update last used if older than 1 day, to minimize queries
    if debug_files:
        update_threshold = now - timedelta(days=1)
        ids_to_update = [df.pk for df in debug_files if df.last_used < update_threshold]
        if ids_to_update:
            await DebugSymbolBundle.objects.filter(pk__in=ids_to_update).aupdate(
                last_used=now
            )

    # Collected/calculated event data while processing
    processing_events: list[ProcessingEvent] = []
    for ingest_event in messages:
        event = ingest_event.payload
        hydrate_stacktrace(event)
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

            processor = JavascriptEventProcessor(
                release_id,
                event,
                [
                    debug_file
                    for debug_file in event_debug_files
                    if debug_file.release_id == release_id
                    or debug_file.data.get("code_file")
                ],
            )
            await sync_to_async(processor.transform)()
        elif isinstance(event, ErrorIssueEventSchema) and event.exception:
            # Projects may not appear in projects_with_data (which is built
            # from release/environment joins), so check has_difs from the
            # annotation when available, otherwise fall back to a direct
            # existence check (e.g. Flutter Android sends no debug_meta).
            _has_difs = next(
                (
                    project["has_difs"]
                    for project in projects_with_data
                    if project["id"] == ingest_event.project_id
                ),
                None,
            )
            if _has_difs is None:
                _has_difs = await DebugInformationFile.objects.filter(
                    project_id=ingest_event.project_id
                ).aexists()
            if _has_difs:
                await sync_to_async(event_difs_resolve_stacktrace)(
                    event, ingest_event.project_id
                )

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
                "threads",
                "breadcrumbs",
                "errors",
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
                uuid=ingest_event.uuid,
            )
        )

    # Build a dict for O(1) lookups instead of iterating the queryset per event
    hash_dict: dict[tuple[int, str], dict] = await _fetch_issue_hashes(
        processing_events, read_only_db
    )

    # Primary fallback: check the primary for hashes not found on the replica.
    # Avoids unnecessary IntegrityErrors caused by replication lag.
    if read_only_db != "default":
        missing = [
            (pe.project_id, pe.issue_hash)
            for pe in processing_events
            if (pe.project_id, pe.issue_hash) not in hash_dict
        ]
        if missing:
            fallback = await _fetch_issue_hashes_raw(missing, "default")
            hash_dict.update(fallback)

    issue_events: list[IssueEvent] = []
    issues_to_reopen = []
    # Group events by time and project for event count statistics
    data_stats: defaultdict[datetime, defaultdict[int, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0, "organization_id": None})
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
            "first_release_id": processing_event.release_id,
            "last_release_id": processing_event.release_id,
        }
        if level := processing_event.level:
            issue_defaults["level"] = level
        hash_obj = hash_dict.get((project_id, processing_event.issue_hash))
        if hash_obj:
            processing_event.issue_id = hash_obj["issue_id"]
            if hash_obj["issue__status"] == EventStatus.RESOLVED:
                resolved_in = hash_obj.get("issue__resolved_in_release_id")
                event_release = processing_event.release_id
                if resolved_in is None or (
                    event_release is not None and resolved_in != event_release
                ):
                    issues_to_reopen.append(hash_obj["issue_id"])

        if not processing_event.issue_id:
            # Project counter + atomic Issue/IssueHash creation needs sync_to_async
            # because Django doesn't support async transaction.atomic() yet
            def _create_issue_and_hash(
                _project_id, _issue_defaults, _processing_event, _processing_events
            ):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO projects_projectcounter (project_id, value)
                        VALUES (%s, 1)
                        ON CONFLICT (project_id) DO UPDATE
                        SET value = projects_projectcounter.value + 1
                        RETURNING value;
                        """,
                        [_project_id],
                    )
                    _issue_defaults["short_id"] = cursor.fetchone()[0]
                try:
                    with transaction.atomic():
                        issue = Issue.objects.create(
                            project_id=_project_id,
                            search_vector=SearchVector(
                                Value(get_search_vector(_processing_event))
                            ),
                            **_issue_defaults,
                        )
                        new_issue_hash = IssueHash.objects.create(
                            issue=issue,
                            value=_processing_event.issue_hash,
                            project_id=_project_id,
                        )
                        check_set_issue_id(
                            _processing_events,
                            issue.project_id,
                            new_issue_hash.value,
                            issue.id,
                        )
                    return issue.id, True
                except IntegrityError:
                    return (
                        IssueHash.objects.get(
                            project_id=_project_id,
                            value=_processing_event.issue_hash,
                        ).issue_id,
                        False,
                    )

            issue_id, created = await sync_to_async(_create_issue_and_hash)(
                project_id, issue_defaults, processing_event, processing_events
            )
            processing_event.issue_id = issue_id
            processing_event.issue_created = created

        hour_received = processing_event.received.replace(
            minute=0, second=0, microsecond=0
        )
        project_stats = data_stats[hour_received][processing_event.project_id]
        project_stats["count"] += 1
        project_stats["organization_id"] = processing_event.organization_id

        if processing_event.issue_id:  # Only count if issue is known
            issue_hourly_stats[hour_received][processing_event.issue_id]["count"] += 1
            issue_hourly_stats[hour_received][processing_event.issue_id][
                "organization_id"
            ] = processing_event.organization_id

        issue_events.append(
            IssueEvent(
                # UUIDv7 id encodes received time (millisecond precision)
                id=UUID7Helper.from_datetime(processing_event.received),
                event_id=processing_event.payload.event_id,
                issue_id=processing_event.issue_id,
                organization_id=processing_event.organization_id,
                type=event_type,
                level=processing_event.level
                if processing_event.level
                else LogLevel.ERROR,
                timestamp=processing_event.payload.timestamp,
                title=remove_bad_chars(processing_event.title),
                transaction=remove_bad_chars(processing_event.transaction),
                data=remove_bad_chars(processing_event.event_data),
                hashes=[processing_event.issue_hash],
                tags=remove_bad_chars(processing_event.event_tags),
                release_id=processing_event.release_id,
            )
        )

    await update_issues(processing_events)

    if settings.CACHE_IS_VALKEY:
        # Add set of issue_ids for alerts to process later
        # Lua script: atomically SADD + EXPIRE in a single round-trip
        driver = caches["default"].get_raw_client()
        issue_ids_bytes = [str(event.issue_id).encode() for event in processing_events]
        await driver.eval(
            "local added = redis.call('SADD', KEYS[1], unpack(ARGV))\n"
            "if added > 0 then\n"
            "  redis.call('EXPIRE', KEYS[1], 3600)\n"
            "end\n"
            "return added",
            [ISSUE_IDS_KEY],
            issue_ids_bytes,
        )

    if issues_to_reopen:
        await Issue.objects.filter(id__in=issues_to_reopen).aupdate(
            status=EventStatus.UNRESOLVED,
            resolved_in_release=None,
        )
        await Notification.objects.filter(issues__in=issues_to_reopen).adelete()

    # ignore_conflicts because we could have an invalid duplicate event_id, received
    if issue_events:

        def _insert_events():
            with connection.cursor() as cursor:
                args_str = ",".join(
                    cursor.mogrify(
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::text[])",
                        (
                            e.id,
                            e.event_id,
                            e.timestamp,
                            e.issue_id,
                            e.organization_id,
                            e.release_id,
                            e.type,
                            e.level,
                            e.title,
                            e.transaction,
                            Jsonb(e.data),
                            Jsonb(e.tags),
                            e.hashes,
                        ),
                    )
                    for e in issue_events
                )
                cursor.execute(
                    "INSERT INTO issue_events_issueevent "
                    "(id, event_id, timestamp, issue_id, organization_id, release_id, "
                    "type, level, title, transaction, data, tags, hashes) "
                    f"VALUES {args_str} ON CONFLICT DO NOTHING"
                )

        await sync_to_async(_insert_events)()

    await update_tags(processing_events)
    await update_statistics(
        data_stats,
        table_name="projects_issueeventprojecthourlystatistic",
    )
    await update_org_statistics(
        issue_hourly_stats,
        table_name="issue_events_issueaggregate",
    )


async def update_statistics(
    stats_data: defaultdict[datetime, defaultdict[int, dict]],
    table_name: StatsTableName,
):
    """
    Generic function to bulk upsert hourly statistics.
    """
    # Runtime check for security
    if table_name not in STATS_TABLE_CONFIG:
        raise ValueError(f"Invalid table_name for statistics update: {table_name}")

    id_column_name = STATS_TABLE_CONFIG[table_name]["id_column"]

    data = []
    for date, inner_dict in stats_data.items():
        for key, stats in inner_dict.items():
            if (organization_id := stats.get("organization_id")) is not None:
                data.append([date, key, organization_id, stats["count"]])

    if not data:
        return

    data.sort(key=itemgetter(0, 1, 2))

    def _execute():
        with connection.cursor() as cursor:
            args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s)", x) for x in data)
            sql = (
                f"INSERT INTO {table_name} (date, {id_column_name}, organization_id, count)\n"
                f"VALUES {args_str}\n"
                f"ON CONFLICT ({id_column_name}, organization_id, date)\n"
                f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count;"
            )
            cursor.execute(sql)

    await sync_to_async(_execute)()


async def update_org_statistics(
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

    def _execute():
        with connection.cursor() as cursor:
            # Prepare the data for a single, bulk INSERT statement
            args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s)", x) for x in data)

            # The ON CONFLICT target must match the composite primary key
            # of (issue_id, organization_id, date)
            conflict_target = f"({id_column_name}, organization_id, date)"

            # Construct the final SQL query
            sql = (
                f"INSERT INTO {table_name} (date, organization_id, {id_column_name}, count)\n"
                f"VALUES {args_str}\n"
                f"ON CONFLICT {conflict_target}\n"
                f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count;"
            )
            cursor.execute(sql)

    await sync_to_async(_execute)()


def _is_error_status(trace_status: str | None) -> bool:
    """Check if a trace status indicates an error."""
    return trace_status in (
        "internal_error",
        "unavailable",
        "deadline_exceeded",
        "unimplemented",
        "permission_denied",
        "unauthenticated",
        "resource_exhausted",
        "data_loss",
        "aborted",
        "failed_precondition",
        "out_of_range",
        "unknown",
    )


async def _update_transaction_group_stats(
    group_durations: dict[int, list[float]],
    group_error_counts: dict[int, int],
    group_org_ids: dict[int, int],
):
    """
    Update TransactionGroup stats using append-only SQL.

    Phase 1: A single UPDATE ... FROM (VALUES ...) atomically merges count,
    error_count, avg_duration, and duration_histogram (integer[] element-wise
    addition). Row locks are held only for this single statement.

    Phase 2: Reads the merged histogram back and recomputes p50/p95 in Python.
    Runs outside the Phase 1 lock — p50/p95 are eventually consistent, which
    is acceptable for approximate dashboard values.
    """
    if not group_durations:
        return

    # Build VALUES list for a single batched UPDATE ... FROM (VALUES ...)
    values_data = []
    for group_id, durations in group_durations.items():
        batch_count = len(durations)
        batch_total = sum(durations)
        error_count = group_error_counts.get(group_id, 0)
        org_id = group_org_ids[group_id]
        histogram_inc = merge_durations(new_histogram(), durations)
        values_data.append(
            (group_id, org_id, batch_count, batch_total, error_count, histogram_inc)
        )

    if not values_data:
        return

    # Sort by (org_id, group_id) for partition-friendly lock ordering
    values_data.sort(key=lambda x: (x[1], x[0]))

    # Phase 1: Atomically merge count, error_count, avg_duration,
    # and duration_histogram via a single UPDATE ... FROM (VALUES ...).
    # Row locks are held only for the duration of this statement.
    def _execute_phase1():
        with connection.cursor() as cursor:
            placeholders = ",".join(
                cursor.mogrify("(%s,%s,%s,%s,%s,%s::integer[])", row)
                for row in values_data
            )
            cursor.execute(
                f"""
                UPDATE performance_transactiongroup AS tg
                SET count = tg.count + v.batch_count,
                    error_count = tg.error_count + v.error_count,
                    avg_duration = CASE
                        WHEN tg.count + v.batch_count > 0
                        THEN (tg.avg_duration * tg.count + v.batch_total)
                             / (tg.count + v.batch_count)
                        ELSE 0
                    END,
                    last_seen = NOW(),
                    duration_histogram = ARRAY(
                        SELECT COALESCE(a, 0) + COALESCE(b, 0)
                        FROM unnest(tg.duration_histogram, v.hist_arr) AS t(a, b)
                    )
                FROM (VALUES {placeholders})
                    AS v(group_id, org_id, batch_count, batch_total, error_count, hist_arr)
                WHERE tg.id = v.group_id
                  AND tg.organization_id = v.org_id
                """
            )

    await sync_to_async(_execute_phase1)()

    # Phase 2: Recompute p50/p95 from the merged histogram.
    # Runs after Phase 1 commits — no row locks held. p50/p95 are
    # eventually consistent (may include other workers' concurrent changes,
    # which makes them more accurate, not less).
    org_ids = list({row[1] for row in values_data})
    group_ids = [row[0] for row in values_data]

    def _fetch_histograms():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, organization_id, count, duration_histogram "
                "FROM performance_transactiongroup "
                "WHERE id = ANY(%s) AND organization_id = ANY(%s)",
                [group_ids, org_ids],
            )
            return cursor.fetchall()

    rows = await sync_to_async(_fetch_histograms)()
    p_updates = []
    for row in rows:
        gid, oid, count, histogram = row
        p50 = percentile_from_histogram(histogram, count, 50)
        p95 = percentile_from_histogram(histogram, count, 95)
        p_updates.append((p50, p95, gid, oid))

    if p_updates:
        p_updates.sort(key=lambda x: (x[3], x[2]))

        def _execute_phase2():
            with connection.cursor() as cursor:
                placeholders = ",".join(
                    cursor.mogrify(
                        "(%s::double precision,%s::double precision,%s,%s)", row
                    )
                    for row in p_updates
                )
                cursor.execute(
                    f"""
                    UPDATE performance_transactiongroup AS tg
                    SET p50 = v.p50, p95 = v.p95
                    FROM (VALUES {placeholders}) AS v(p50, p95, group_id, org_id)
                    WHERE tg.id = v.group_id
                      AND tg.organization_id = v.org_id
                    """
                )

        await sync_to_async(_execute_phase2)()


TagStats = defaultdict[
    datetime,
    defaultdict[int, defaultdict[int, defaultdict[int, dict]]],
]


async def update_tags(processing_events: list[ProcessingEvent]):
    # Truncate long values and strip NUL bytes
    for processing_event in processing_events:
        processing_event.event_tags = {
            remove_bad_chars(str(key))[:MAX_TAG_LENGTH]: remove_bad_chars(str(value))[
                :MAX_TAG_LENGTH
            ]
            for key, value in processing_event.event_tags.items()
        }
    keys = sorted({key for d in processing_events for key in d.event_tags.keys()})
    values = sorted(
        {value for d in processing_events for value in d.event_tags.values()}
    )

    if not keys:
        return

    await TagKey.objects.abulk_create(
        [TagKey(key=key) for key in keys], ignore_conflicts=True
    )
    await TagValue.objects.abulk_create(
        [TagValue(value=value) for value in values], ignore_conflicts=True
    )

    # Postgres cannot return ids with ignore_conflicts
    def _fetch_tag_ids():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, key FROM issue_events_tagkey WHERE key = ANY(%s)",
                [keys],
            )
            tk = {row[1]: row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT id, value FROM issue_events_tagvalue WHERE value = ANY(%s)",
                [values],
            )
            tv = {row[1]: row[0] for row in cursor.fetchall()}
            return tk, tv

    tag_keys, tag_values = await sync_to_async(_fetch_tag_ids)()

    tag_stats: TagStats = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(lambda: {"count": 0, "organization_id": None})
            )
        )
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
            bucket = tag_stats[minute_received][processing_event.issue_id][key_id][
                value_id
            ]
            bucket["count"] += 1
            bucket["organization_id"] = processing_event.organization_id

    if not tag_stats:
        return

    # Sort to mitigate deadlocks
    data = []
    for date, d1 in tag_stats.items():
        for issue_id, d2 in d1.items():
            for key_id, d3 in d2.items():
                for value_id, stats in d3.items():
                    data.append(
                        [
                            date,
                            issue_id,
                            stats["organization_id"],
                            key_id,
                            value_id,
                            stats["count"],
                        ]
                    )

    data.sort(key=itemgetter(0, 1, 2, 3, 4))

    def _execute():
        with connection.cursor() as cursor:
            args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s,%s)", x) for x in data)
            sql = (
                "INSERT INTO issue_events_issuetag (date, issue_id, organization_id, tag_key_id, tag_value_id, count)\n"
                f"VALUES {args_str}\n"
                "ON CONFLICT (issue_id, organization_id, tag_key_id, tag_value_id, date)\n"
                "DO UPDATE SET count = issue_events_issuetag.count + EXCLUDED.count;"
            )
            cursor.execute(sql)

    await sync_to_async(_execute)()


# Transactions


class _TxnGroupRef:
    """Lightweight stand-in for TransactionGroup with only the fields needed downstream."""

    __slots__ = ("id", "organization_id")

    def __init__(self, id: int, organization_id: int):
        self.id = id
        self.organization_id = organization_id


async def _fetch_transaction_groups(
    keys: list[tuple[int, str, str, str]], db_alias: str
) -> dict[tuple[int, str, str, str], _TxnGroupRef]:
    """Fetch TransactionGroup id/organization_id via raw SQL VALUES lookup."""
    if not keys:
        return {}

    def _execute():
        with connections[db_alias].cursor() as cursor:
            values_str = ",".join(cursor.mogrify("(%s,%s,%s,%s)", k) for k in keys)
            cursor.execute(
                f"""
                SELECT id, organization_id, project_id, transaction, op, method
                FROM performance_transactiongroup
                WHERE (project_id, transaction, op, method) IN (VALUES {values_str})
                """
            )
            return cursor.fetchall()

    rows = await sync_to_async(_execute)()
    return {
        (row[2], row[3], row[4], row[5]): _TxnGroupRef(
            id=row[0], organization_id=row[1]
        )
        for row in rows
    }


async def process_transaction_events(
    ingest_events: list[InterchangeTransactionEvent], read_only_db: str = "default"
):
    now = timezone.now()

    projects_to_update = {
        msg.project_id for msg in ingest_events if msg.update_first_event
    }
    if projects_to_update:
        await Project.objects.filter(
            id__in=projects_to_update, first_event__isnull=True
        ).aupdate(first_event=now)

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
    await _get_or_create_related_models(
        release_set, environment_set, project_set, read_only_db
    )

    # 1. Parse event data and collect unique group keys
    GroupKey = tuple[int, str, str, str]  # (project_id, transaction, op, method)
    event_data: list[
        tuple[InterchangeTransactionEvent, str, str, str | None, GroupKey]
    ] = []
    unique_keys: dict[GroupKey, int] = {}  # key -> organization_id

    for ingest_event in ingest_events:
        event = ingest_event.payload
        contexts = event.contexts
        request = event.request
        op = ""
        trace_status = None
        if isinstance(contexts, dict):
            trace = contexts.get("trace", {})
            if isinstance(trace, dict):
                op = str(trace.get("op", ""))
                trace_status = trace.get("status")
        method = ""
        if request and request.method:
            method = request.method

        transaction_name = event.transaction[:1024]
        key: GroupKey = (ingest_event.project_id, transaction_name, op, method)
        unique_keys.setdefault(key, ingest_event.organization_id)
        event_data.append((ingest_event, transaction_name, op, trace_status, key))

    # 2. Batch fetch existing TransactionGroups (single query)
    existing: dict[GroupKey, _TxnGroupRef] = {}
    if unique_keys:
        existing = await _fetch_transaction_groups(
            list(unique_keys.keys()), read_only_db
        )

    # Batch create any missing groups
    missing_keys = [k for k in unique_keys if k not in existing]
    if missing_keys:
        new_groups = [
            TransactionGroup(
                project_id=k[0],
                transaction=k[1],
                op=k[2],
                method=k[3],
                organization_id=unique_keys[k],
                first_seen=now,
                last_seen=now,
            )
            for k in missing_keys
        ]
        await TransactionGroup.objects.abulk_create(new_groups, ignore_conflicts=True)
        # Re-fetch to get IDs (bulk_create with ignore_conflicts doesn't set PKs)
        if missing_keys:
            refetched = await _fetch_transaction_groups(missing_keys, "default")
            existing.update(refetched)

    # 3. Collect durations, error counts, and spans per group
    collect_spans = is_duckdb_available()
    if collect_spans:
        from apps.performance.models import SpanStaging
    group_durations: dict[int, list[float]] = defaultdict(list)
    group_error_counts: dict[int, int] = defaultdict(int)
    group_org_ids: dict[int, int] = {}
    span_rows: list = []

    data_stats: defaultdict[datetime, defaultdict[int, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"count": 0, "organization_id": None})
    )

    for ingest_event, transaction_name, op, trace_status, key in event_data:
        event = ingest_event.payload
        group = existing.get(key)
        if not group:
            continue  # Should not happen after bulk_create

        # Calculate duration
        duration_ms = 0.0
        if event.timestamp and event.start_timestamp:
            delta = event.timestamp - event.start_timestamp
            duration_ms = max(0.0, delta.total_seconds() * 1000)

        group_durations[group.id].append(duration_ms)
        group_org_ids[group.id] = group.organization_id

        if _is_error_status(trace_status):
            group_error_counts[group.id] += 1

        # Hourly project statistics
        hour_received = event.start_timestamp.replace(minute=0, second=0, microsecond=0)
        project_stats = data_stats[hour_received][ingest_event.project_id]
        project_stats["count"] += 1
        project_stats["organization_id"] = ingest_event.organization_id

        if not collect_spans:
            continue

        # Extract spans (capped to prevent abuse from oversized transactions)
        event_id_hex = event.event_id.hex if event.event_id else ""
        if event.spans:
            for span in event.spans[:MAX_SPANS_PER_TRANSACTION]:
                span_duration_ms = 0.0
                if span.timestamp and span.start_timestamp:
                    span_delta = span.timestamp - span.start_timestamp
                    span_duration_ms = max(0.0, span_delta.total_seconds() * 1000)

                description = parameterize_description(span.op, span.description)

                span_rows.append(
                    SpanStaging(
                        organization_id=ingest_event.organization_id,
                        project_id=ingest_event.project_id,
                        transaction_name=remove_bad_chars(transaction_name),
                        span_id=span.span_id[:32],
                        transaction_id=event_id_hex[:32],
                        op=remove_bad_chars(span.op[:255]),
                        description=remove_bad_chars(description),
                        duration=span_duration_ms,
                        timestamp=span.start_timestamp or event.start_timestamp,
                    )
                )

    # 4. Update TransactionGroup stats (append-only, no read-modify-write)
    await _update_transaction_group_stats(
        group_durations, group_error_counts, group_org_ids
    )

    # 5. Bulk insert span staging rows
    if span_rows:
        await SpanStaging.objects.abulk_create(span_rows, batch_size=1000)

    # 6. Update hourly project statistics
    await update_statistics(
        data_stats,
        table_name="projects_transactioneventprojecthourlystatistic",
    )
