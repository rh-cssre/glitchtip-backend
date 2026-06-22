import os
from collections import defaultdict
from datetime import datetime, timedelta
from operator import itemgetter
from typing import Any, Literal
from urllib.parse import ParseResult, urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import caches
from django.db import connections, transaction
from django.db.models import Q
from django.db.utils import IntegrityError
from django.utils import timezone
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet
from ninja import Schema
from psycopg.types.json import Jsonb
from user_agents import parse

from apps.alerts.constants import ISSUE_IDS_KEY
from apps.difs.tasks import event_difs_resolve_stacktrace
from apps.issue_events.constants import MAX_TAG_LENGTH, EventStatus, LogLevel
from apps.issue_events.models import IssueEvent, IssueEventType
from apps.performance.histogram import (
    merge_durations,
    new_histogram,
    percentile_from_histogram,
)
from apps.performance.parameterize import parameterize_description
from apps.shared.raw_sql import (
    execute,
    execute_mogrified_values,
    execute_unnest,
    fetchall,
    fetchall_mogrified_values,
    fetchall_unnest,
)
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
        project_ids = list(project_set)
        release_versions = list(release_version_set)
        environment_names = list(environment_name_set)
        columns, rows = await fetchall(
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
            db_alias=read_only_db,
        )
        projects_with_data = [dict(zip(columns, row)) for row in rows]

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
                organization_id=processing_event.organization_id,
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
                value.organization_id,
                value.added_count,
                value.last_seen,
                value.last_release_id,
                value.search_vector,
            )
            for issue_id, value in issues_to_update.items()
        ],
        key=itemgetter(0),
    )

    max_lexemes = settings.SEARCH_MAX_LEXEMES
    # Single hot-path write: the IssueIndex leaf carries the per-event
    # columns (count/last_seen/last_release) and the full-text document, so the
    # Issue table is not touched on event ingest. Append to rows that already
    # exist, insert the rest. The UPDATE is filtered on organization_id (the
    # hash partition key) so Postgres prunes partitions. An issue with no leaf
    # row yet (created by an older release, or not backfilled) is created here on
    # its next event — both the stats and search self-heal.
    sql = (
        "WITH v AS ("
        "SELECT * FROM unnest(%s::bigint[], %s::int[], %s::int[], %s::timestamptz[], %s::bigint[], %s::text[]) "
        "AS t(id, org_id, added_count, last_seen, last_release_id, new_text)"
        "), upd AS ("
        "UPDATE issue_events_issueindex t SET "
        "count = t.count + v.added_count, "
        "last_seen = GREATEST(t.last_seen, v.last_seen), "
        "last_release_id = COALESCE(v.last_release_id, t.last_release_id), "
        "fts_document = "
        f"append_and_limit_tsvector(t.fts_document, v.new_text, {max_lexemes}, 'english'::regconfig) "
        "FROM v WHERE t.issue_id = v.id AND t.organization_id = v.org_id "
        "RETURNING t.issue_id"
        ") "
        "INSERT INTO issue_events_issueindex "
        "(issue_id, organization_id, count, last_seen, last_release_id, fts_document) "
        "SELECT v.id, v.org_id, v.added_count, v.last_seen, v.last_release_id, "
        "to_tsvector('english'::regconfig, v.new_text) "
        "FROM v WHERE NOT EXISTS (SELECT 1 FROM upd WHERE upd.issue_id = v.id) "
        "ON CONFLICT (issue_id, organization_id) DO NOTHING"
    )
    await execute_unnest(sql, list(data))


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
    env_to_create = [
        (name, organization_id)
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

    if env_to_create:
        await execute_mogrified_values(
            sql_template=(
                "INSERT INTO environments_environment (name, organization_id, created) "
                "VALUES {values} "
                "ON CONFLICT (organization_id, name) DO NOTHING"
            ),
            values_fragment="(%s,%s,NOW())",
            value_params=list(env_to_create),
        )

        _, env_rows = await fetchall_mogrified_values(
            "SELECT id, name, organization_id FROM environments_environment "
            "WHERE (name, organization_id) IN (VALUES {values})",
            "(%s,%s)",
            list(env_to_create),
            db_alias="default",
        )
        environment_projects = []
        for env_id, env_name, env_org_id in env_rows:
            pid = next(
                project_id
                for (name, project_id, organization_id) in environment_set
                if env_name == name and env_org_id == organization_id
            )
            environment_projects.append((pid, env_id))
        if environment_projects:
            await execute_mogrified_values(
                sql_template=(
                    "INSERT INTO environments_environmentproject "
                    "(project_id, environment_id, is_hidden, created) "
                    "VALUES {values} "
                    "ON CONFLICT (project_id, environment_id) DO NOTHING"
                ),
                values_fragment="(%s,%s,false,NOW())",
                value_params=environment_projects,
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
        (release_name, organization_id)
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
        # Create database records for any release that doesn't exist.
        # data='{}' satisfies the NOT NULL JSONB column; commit_count and
        # deploy_count default to 0 in the model and are required NOT NULL.
        await execute_mogrified_values(
            sql_template=(
                "INSERT INTO releases_release "
                "(version, organization_id, created, data, commit_count, deploy_count) "
                "VALUES {values} "
                "ON CONFLICT (organization_id, version) DO NOTHING"
            ),
            values_fragment="(%s,%s,NOW(),'{}'::jsonb,0,0)",
            value_params=list(releases_to_create),
        )

        _, release_rows = await fetchall_mogrified_values(
            "SELECT id, version, organization_id FROM releases_release "
            "WHERE (version, organization_id) IN (VALUES {values})",
            "(%s,%s)",
            list(releases_to_create),
            db_alias="default",
        )
        release_project_pairs = [
            (
                rel_id,
                next(
                    project_id
                    for (version, project_id, organization_id) in release_set
                    if rel_version == version and rel_org_id == organization_id
                ),
            )
            for rel_id, rel_version, rel_org_id in release_rows
        ]
        if release_project_pairs:
            await execute_mogrified_values(
                sql_template=(
                    "INSERT INTO releases_release_projects (release_id, project_id) "
                    "VALUES {values} "
                    "ON CONFLICT (release_id, project_id) DO NOTHING"
                ),
                values_fragment="(%s,%s)",
                value_params=release_project_pairs,
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
    """Fetch IssueHash rows with issue status via JOIN unnest lookup."""
    if not pairs:
        return {}

    sql = """
        SELECT ih.project_id, ih.value, ih.issue_id,
               si.status AS issue__status,
               i.resolved_in_release_id AS issue__resolved_in_release_id
        FROM unnest(%s::bigint[], %s::uuid[]) AS k(project_id, value)
        JOIN issue_events_issuehash ih
            ON ih.project_id = k.project_id AND ih.value = k.value
        JOIN issue_events_issue i ON i.id = ih.issue_id
        JOIN issue_events_issueindex si ON si.issue_id = ih.issue_id
    """
    columns, rows = await fetchall_unnest(sql, list(pairs), db_alias=db_alias)
    dicts = [dict(zip(columns, row)) for row in rows]
    return {(h["project_id"], h["value"].hex): h for h in dicts}


async def _fetch_issue_hashes(
    processing_events: list[ProcessingEvent], db_alias: str
) -> dict[tuple[int, str], dict]:
    """Collect unique (project_id, issue_hash) pairs and fetch via raw SQL."""
    pairs = list(
        {(pe.project_id, pe.issue_hash) for pe in processing_events if pe.issue_hash}
    )
    return await _fetch_issue_hashes_raw(pairs, db_alias)


async def _create_issue_and_hash(
    project_id: int,
    issue_defaults: dict,
    processing_event: ProcessingEvent,
    processing_events: list[ProcessingEvent],
) -> tuple[int, bool]:
    """Atomically create an Issue + IssueHash and return ``(issue_id, created)``.

    The whole unit — project-counter upsert, then the Issue, IssueHash, and
    IssueIndex (the issue's hot/queryable projection plus its full-text
    document) writes wrapped in ``transaction.atomic()`` — runs inside a single
    ``sync_to_async`` hop over Django's sync connection. This is deliberate:
    that sync connection is thread-local, and this async worker shares it
    across concurrently running tasks. Holding a transaction open across an
    ``await`` would let a sibling task close or poison that shared connection
    mid-block, cascading as "Cannot open a new connection in an atomic block" /
    TransactionManagementError. Keeping the transaction inside one synchronous
    call means it never spans an await, so siblings serialise before/after it
    on the executor thread and can't interfere.

    The unique on ``(project_id, value)`` lets concurrent ingest of the same
    hash race; the loser catches IntegrityError, reads back the winner's id,
    and returns ``created=False``. The project counter upsert runs before the
    atomic block so its value is consumed even on the IntegrityError path.
    """
    search_vector_str = get_search_vector(processing_event)

    def _create_issue_and_hash_sync() -> tuple[int, bool]:
        conn = connections["default"]
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects_projectcounter (project_id, value)
                VALUES (%s, 1)
                ON CONFLICT (project_id) DO UPDATE
                SET value = projects_projectcounter.value + 1
                RETURNING value
                """,
                [project_id],
            )
            short_id = cursor.fetchone()[0]
        try:
            with transaction.atomic():
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO issue_events_issue (
                            project_id, type, title, metadata,
                            first_seen, first_release_id,
                            short_id, is_public, is_deleted, culprit
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, false, false, NULL
                        )
                        RETURNING id
                        """,
                        [
                            project_id,
                            issue_defaults["type"],
                            issue_defaults["title"],
                            Jsonb(issue_defaults["metadata"]),
                            issue_defaults["first_seen"],
                            issue_defaults.get("first_release_id"),
                            short_id,
                        ],
                    )
                    issue_id = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO issue_events_issuehash (issue_id, project_id, value)
                        VALUES (%s, %s, %s::uuid)
                        """,
                        [issue_id, project_id, processing_event.issue_hash],
                    )
                    # Write the IssueIndex leaf: the hot/queryable projection
                    # of the issue (count/last_seen/status/level/last_release)
                    # plus the sole full-text document. count starts at 1;
                    # status defaults UNRESOLVED.
                    cursor.execute(
                        """
                        INSERT INTO issue_events_issueindex (
                            issue_id, organization_id, last_release_id, last_seen,
                            count, status, level, fts_document
                        )
                        VALUES (
                            %s, %s, %s, %s, 1, %s, %s, to_tsvector('english', %s)
                        )
                        """,
                        [
                            issue_id,
                            processing_event.organization_id,
                            issue_defaults.get("last_release_id"),
                            issue_defaults["last_seen"],
                            EventStatus.UNRESOLVED,
                            issue_defaults.get("level", LogLevel.ERROR),
                            search_vector_str,
                        ],
                    )
            check_set_issue_id(
                processing_events,
                project_id,
                processing_event.issue_hash,
                issue_id,
            )
            return issue_id, True
        except IntegrityError:
            # Concurrent writer won the (project_id, value) race; read its id.
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT issue_id FROM issue_events_issuehash
                    WHERE project_id = %s AND value = %s::uuid
                    """,
                    [project_id, processing_event.issue_hash],
                )
                return cursor.fetchone()[0], False

    return await sync_to_async(_create_issue_and_hash_sync)()


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
        await execute(
            """
            UPDATE projects_project SET first_event = %s
            WHERE id = ANY(%s) AND first_event IS NULL
            """,
            [timezone.now(), list(projects_to_update)],
        )

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
        AsyncQuerySet(model=DebugSymbolBundle, using=read_only_db)
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
            await execute(
                "UPDATE sourcecode_debugsymbolbundle SET last_used = %s "
                "WHERE id = ANY(%s)",
                [now, ids_to_update],
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
                _, exists_rows = await fetchall(
                    "SELECT EXISTS("
                    "SELECT 1 FROM difs_debuginformationfile WHERE project_id = %s"
                    ")",
                    [ingest_event.project_id],
                )
                _has_difs = exists_rows[0][0]
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
                    # (issue_id, organization_id) so the leaf reopen below prunes
                    # to a single hash partition.
                    issues_to_reopen.append(
                        (hash_obj["issue_id"], processing_event.organization_id)
                    )

        if not processing_event.issue_id:
            issue_id, created = await _create_issue_and_hash(
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
        # issues_to_reopen holds (issue_id, organization_id) pairs.
        reopen_ids = [p[0] for p in issues_to_reopen]
        reopen_orgs = [p[1] for p in issues_to_reopen]
        # status lives on the IssueIndex leaf; resolved_in_release stays on
        # Issue. The leaf update joins on (issue_id, organization_id) so Postgres
        # prunes to single hash partitions instead of scanning all of them.
        await execute(
            "UPDATE issue_events_issueindex si SET status = %s "
            "FROM unnest(%s::bigint[], %s::int[]) AS v(issue_id, org_id) "
            "WHERE si.issue_id = v.issue_id AND si.organization_id = v.org_id",
            [EventStatus.UNRESOLVED, reopen_ids, reopen_orgs],
        )
        await execute(
            "UPDATE issue_events_issue SET resolved_in_release_id = NULL "
            "WHERE id = ANY(%s)",
            [reopen_ids],
        )
        # Notification.issues is a Django ManyToManyField; the through-table
        # FKs aren't ON DELETE CASCADE in Postgres (Django emulates that in
        # ORM-land), so do the cascade ourselves in a single CTE round-trip:
        # snapshot the matching notification ids, drop the through rows,
        # then drop the notifications.
        await execute(
            """
            WITH notification_ids AS (
                SELECT DISTINCT notification_id
                FROM alerts_notification_issues
                WHERE issue_id = ANY(%s)
            ),
            deleted_through AS (
                DELETE FROM alerts_notification_issues
                WHERE notification_id IN (SELECT notification_id FROM notification_ids)
            )
            DELETE FROM alerts_notification
            WHERE id IN (SELECT notification_id FROM notification_ids)
            """,
            [reopen_ids],
        )

    # ignore_conflicts because we could have an invalid duplicate event_id, received
    if issue_events:
        # IssueEvent.hashes is text[] but each row carries exactly one hash
        # (built at line 1049 above), so unnest a flat text[] and wrap with
        # ARRAY[hash] in the SELECT — the column-major form sidesteps the
        # 65535 bind-param cap and skips per-row mogrify.
        await execute_unnest(
            sql=(
                "INSERT INTO issue_events_issueevent "
                "(id, event_id, timestamp, issue_id, organization_id, release_id, "
                "type, level, title, transaction, data, tags, hashes) "
                "SELECT id, event_id, ts, issue_id, organization_id, release_id, "
                "type, level, title, transaction, data, tags, ARRAY[hash] "
                "FROM unnest("
                "%s::uuid[], %s::uuid[], %s::timestamptz[], %s::bigint[], "
                "%s::bigint[], %s::bigint[], %s::smallint[], %s::smallint[], "
                "%s::text[], %s::text[], %s::jsonb[], %s::jsonb[], %s::text[]"
                ") AS t(id, event_id, ts, issue_id, organization_id, release_id, "
                "type, level, title, transaction, data, tags, hash) "
                "ON CONFLICT DO NOTHING"
            ),
            value_params=[
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
                    e.hashes[0] if e.hashes else "",
                )
                for e in issue_events
            ],
        )

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

    await execute_unnest(
        sql=(
            f"INSERT INTO {table_name} "
            f"(date, {id_column_name}, organization_id, count) "
            "SELECT * FROM unnest(%s::timestamptz[], %s::bigint[], %s::bigint[], %s::int[]) "
            f"ON CONFLICT ({id_column_name}, organization_id, date) "
            f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count"
        ),
        value_params=data,
    )


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

    await execute_unnest(
        sql=(
            f"INSERT INTO {table_name} "
            f"(date, organization_id, {id_column_name}, count) "
            "SELECT * FROM unnest(%s::timestamptz[], %s::bigint[], %s::bigint[], %s::int[]) "
            f"ON CONFLICT ({id_column_name}, organization_id, date) "
            f"DO UPDATE SET count = {table_name}.count + EXCLUDED.count"
        ),
        value_params=data,
    )


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

    await execute_mogrified_values(
        sql_template="""
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
            FROM (VALUES {values})
                AS v(group_id, org_id, batch_count, batch_total, error_count, hist_arr)
            WHERE tg.id = v.group_id
              AND tg.organization_id = v.org_id
        """,
        values_fragment="(%s,%s,%s,%s,%s,%s::integer[])",
        value_params=values_data,
    )

    # Phase 2: Recompute p50/p95 from the merged histogram.
    # Runs after Phase 1 commits — no row locks held. p50/p95 are
    # eventually consistent (may include other workers' concurrent changes,
    # which makes them more accurate, not less).
    org_ids = list({row[1] for row in values_data})
    group_ids = [row[0] for row in values_data]

    _, rows = await fetchall(
        "SELECT id, organization_id, count, duration_histogram "
        "FROM performance_transactiongroup "
        "WHERE id = ANY(%s) AND organization_id = ANY(%s)",
        [group_ids, org_ids],
    )
    p_updates = []
    for row in rows:
        gid, oid, count, histogram = row
        p50 = percentile_from_histogram(histogram, count, 50)
        p95 = percentile_from_histogram(histogram, count, 95)
        p_updates.append((p50, p95, gid, oid))

    if p_updates:
        p_updates.sort(key=lambda x: (x[3], x[2]))

        await execute_unnest(
            sql="""
                UPDATE performance_transactiongroup AS tg
                SET p50 = v.p50, p95 = v.p95
                FROM unnest(%s::double precision[], %s::double precision[],
                            %s::bigint[], %s::int[])
                    AS v(p50, p95, group_id, org_id)
                WHERE tg.id = v.group_id
                  AND tg.organization_id = v.org_id
            """,
            value_params=p_updates,
        )


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

    await execute(
        "INSERT INTO issue_events_tagkey (key) "
        "SELECT * FROM UNNEST(%s::varchar[]) ON CONFLICT DO NOTHING",
        [keys],
    )
    await execute(
        "INSERT INTO issue_events_tagvalue (value) "
        "SELECT * FROM UNNEST(%s::varchar[]) ON CONFLICT DO NOTHING",
        [values],
    )

    # Postgres cannot return ids with ignore_conflicts

    _, tk_rows = await fetchall(
        "SELECT id, key FROM issue_events_tagkey WHERE key = ANY(%s)",
        [keys],
    )
    tag_keys = {row[1]: row[0] for row in tk_rows}
    _, tv_rows = await fetchall(
        "SELECT id, value FROM issue_events_tagvalue WHERE value = ANY(%s)",
        [values],
    )
    tag_values = {row[1]: row[0] for row in tv_rows}

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

    await execute_unnest(
        sql=(
            "INSERT INTO issue_events_issuetag "
            "(date, issue_id, organization_id, tag_key_id, tag_value_id, count) "
            "SELECT * FROM unnest("
            "%s::timestamptz[], %s::bigint[], %s::bigint[], "
            "%s::int[], %s::int[], %s::int[]"
            ") "
            "ON CONFLICT (issue_id, organization_id, tag_key_id, tag_value_id, date) "
            "DO UPDATE SET count = issue_events_issuetag.count + EXCLUDED.count;"
        ),
        value_params=data,
    )


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
    """Fetch TransactionGroup id/organization_id via JOIN unnest lookup."""
    if not keys:
        return {}

    _, rows = await fetchall_unnest(
        sql=(
            "SELECT tg.id, tg.organization_id, tg.project_id, tg.transaction, tg.op, tg.method "
            "FROM unnest(%s::int[], %s::text[], %s::text[], %s::text[]) "
            "    AS k(project_id, transaction, op, method) "
            "JOIN performance_transactiongroup tg "
            "    ON tg.project_id = k.project_id "
            "   AND tg.transaction = k.transaction "
            "   AND tg.op = k.op "
            "   AND tg.method = k.method"
        ),
        value_params=keys,
        db_alias=db_alias,
    )
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
        await execute(
            "UPDATE projects_project SET first_event = %s "
            "WHERE id = ANY(%s) AND first_event IS NULL",
            [now, list(projects_to_update)],
        )

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
        new_group_rows = [
            (
                k[0],  # project_id
                k[1],  # transaction
                k[2],  # op
                k[3],  # method
                unique_keys[k],  # organization_id
                now,  # first_seen
                now,  # last_seen
            )
            for k in missing_keys
        ]
        await execute_unnest(
            sql=(
                "INSERT INTO performance_transactiongroup "
                "(project_id, transaction, op, method, organization_id, "
                "first_seen, last_seen) "
                "SELECT * FROM unnest("
                "%s::int[], %s::text[], %s::text[], %s::text[], "
                "%s::int[], %s::timestamptz[], %s::timestamptz[]"
                ") "
                "ON CONFLICT (transaction, project_id, op, method, organization_id) "
                "DO NOTHING"
            ),
            value_params=new_group_rows,
        )
        # Re-fetch to get IDs (the INSERT above can't return ids when
        # rows are skipped via ON CONFLICT)
        refetched = await _fetch_transaction_groups(missing_keys, "default")
        existing.update(refetched)

    # 3. Collect durations, error counts, and spans per group
    collect_spans = is_duckdb_available()
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
                span_timestamp = span.start_timestamp or event.start_timestamp
                # SpanStaging.id is a server-time UUIDv7. It is the table's
                # RANGE-partition key and promotion's insertion-order cursor
                # (id < cutoff), so it must track ingestion time, not the
                # client clock: staging keeps only a short partition horizon,
                # and a skewed/backdated client timestamp would route the row
                # to a nonexistent partition (IntegrityError, failing the
                # whole batch). The real event time is preserved in the
                # timestamp column below, which is what promotion buckets and
                # garbage-filters on.
                span_rows.append(
                    (
                        UUID7Helper.from_datetime(),
                        ingest_event.organization_id,
                        ingest_event.project_id,
                        span_duration_ms,
                        span_timestamp,
                        remove_bad_chars(transaction_name),
                        span.span_id[:32],
                        event_id_hex[:32],
                        remove_bad_chars(span.op[:255]),
                        remove_bad_chars(description),
                    )
                )

    # 4. Update TransactionGroup stats (append-only, no read-modify-write)
    await _update_transaction_group_stats(
        group_durations, group_error_counts, group_org_ids
    )

    # 5. Bulk insert span staging rows
    if span_rows:
        await execute_unnest(
            sql=(
                "INSERT INTO performance_spanstaging "
                "(id, organization_id, project_id, duration, timestamp, "
                "transaction_name, span_id, transaction_id, op, description) "
                "SELECT * FROM unnest("
                "%s::uuid[], %s::int[], %s::int[], %s::double precision[], "
                "%s::timestamptz[], %s::text[], %s::text[], %s::text[], "
                "%s::text[], %s::text[]"
                ")"
            ),
            value_params=span_rows,
        )

    # 6. Update hourly project statistics
    await update_statistics(
        data_stats,
        table_name="projects_transactioneventprojecthourlystatistic",
    )
