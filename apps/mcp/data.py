from datetime import datetime, timedelta, timezone
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db.models import QuerySet

from apps.alerts.api import get_project_alert_queryset
from apps.alerts.models import ProjectAlert
from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent
from apps.issue_events.services import filter_issue_list, is_uuid7
from apps.issue_events.services import get_queryset as get_issues_qs
from apps.logs.api import LogEventRow, query_logs_combined
from apps.logs.constants import parse_level_filters
from apps.organizations_ext.models import Organization
from apps.organizations_ext.queryset_utils import (
    get_organization_for_user,
    get_organizations_queryset,
)
from apps.performance.models import TransactionGroup
from apps.projects.api import get_projects_queryset
from apps.projects.models import Project
from apps.releases.models import Release
from apps.uptime.api import get_monitor_queryset
from apps.uptime.models import Monitor
from glitchtip.api.pagination import AsyncLinkHeaderPagination
from glitchtip.partition_manager import UUID7Helper


def _apply_compliance_filter(qs: QuerySet) -> QuerySet:
    """Hook for future data-filtering requirements. Returns queryset unchanged."""
    return qs


async def get_organizations(user_id: int) -> list[Organization]:
    qs = get_organizations_queryset(user_id)
    qs = _apply_compliance_filter(qs)
    return [org async for org in qs]


async def get_projects(user_id: int, organization_slug: str) -> list[Project]:
    qs = get_projects_queryset(user_id, organization_slug=organization_slug)
    qs = qs.select_related("organization")
    qs = _apply_compliance_filter(qs)
    return [p async for p in qs.order_by("name")]


async def get_issues(
    user_id: int,
    organization_slug: str,
    project_slug: str | None = None,
    query: str | None = None,
    sort: str | None = None,
    limit: int = 25,
) -> list[Issue]:
    qs = await get_issues_qs(user_id, organization_slug, project_slug)
    filters = {}
    if query:
        filters["query"] = query
    qs = filter_issue_list(qs, filters, sort=sort)
    qs = _apply_compliance_filter(qs)
    limit = min(limit, AsyncLinkHeaderPagination.max_page_size)
    return [issue async for issue in qs[:limit]]


async def get_issue(user_id: int, issue_id: int) -> Issue | None:
    qs = Issue.objects.filter(project__organization__users=user_id).select_related(
        "project", "resolved_in_release"
    )
    qs = _apply_compliance_filter(qs)
    return await qs.filter(id=issue_id).afirst()


async def get_latest_event(user_id: int, issue_id: int) -> IssueEvent | None:
    issue = await Issue.objects.filter(
        id=issue_id, project__organization__users=user_id
    ).select_related("project__organization").afirst()
    if not issue:
        return None
    qs = IssueEvent.objects.filter(
        issue_id=issue_id,
        organization__users=user_id,
    ).order_by("-id")
    qs = _apply_compliance_filter(qs)
    event = await qs.afirst()
    if event:
        return event

    # Fall back to cold storage
    from apps.issue_events.cold_storage import is_duckdb_available, query_cold_events

    if not is_duckdb_available():
        return None

    cold_events = await sync_to_async(query_cold_events)(
        organization_id=issue.project.organization_id,
        start_dt=datetime.min.replace(tzinfo=timezone.utc),
        end_dt=datetime.now(timezone.utc),
        issue_id=issue_id,
        limit=1,
    )
    if not cold_events:
        return None

    event = cold_events[0]
    event.issue = issue
    return event


async def get_event(
    user_id: int, event_id: str, organization_slug: str | None = None
) -> IssueEvent | None:
    """Look up an event by UUID. Accepts either the server-generated UUIDv7 id
    or the client-provided Sentry SDK event_id (UUIDv4).
    Includes related issue and project for full context.

    When organization_slug is provided, queries are scoped to that org for
    better partition pruning in both Postgres and cold storage.
    """
    try:
        uuid_val = UUID(event_id)
    except ValueError:
        return None

    qs = IssueEvent.objects.filter(
        organization__users=user_id,
    ).select_related("issue", "issue__project")
    if organization_slug:
        qs = qs.filter(organization__slug=organization_slug)
    qs = _apply_compliance_filter(qs)

    uuid7 = is_uuid7(uuid_val)
    if uuid7:
        event = await qs.filter(id=uuid_val).afirst()
    else:
        event = await qs.filter(event_id=uuid_val).afirst()
    if event:
        return event

    # Fall back to cold storage
    from apps.issue_events.cold_storage import (
        get_event_from_cold,
        is_duckdb_available,
        query_cold_events,
    )

    if not is_duckdb_available():
        return None

    # Resolve org_ids to search
    org_qs = Organization.objects.filter(users=user_id)
    if organization_slug:
        org_qs = org_qs.filter(slug=organization_slug)
    org_ids = [oid async for oid in org_qs.values_list("id", flat=True)]

    if uuid7:
        # UUIDv7: extract timestamp to target the exact parquet file
        try:
            event_time = UUID7Helper.extract_datetime(uuid_val)
        except ValueError:
            return None
        for org_id in org_ids:
            cold_event = await sync_to_async(get_event_from_cold)(
                org_id, uuid_val, event_time
            )
            if cold_event:
                return await _attach_issue(cold_event, user_id)
    else:
        # UUIDv4 (sentry SDK event_id): scan recent cold storage with time bound
        now = datetime.now(timezone.utc)
        start_dt = now - timedelta(days=90)
        for org_id in org_ids:
            cold_events = await sync_to_async(query_cold_events)(
                organization_id=org_id,
                start_dt=start_dt,
                end_dt=now,
                event_id=uuid_val,
                limit=1,
            )
            if cold_events:
                return await _attach_issue(cold_events[0], user_id)

    return None


async def _attach_issue(cold_event, user_id: int):
    """Attach the issue relation to a cold storage event, verifying access."""
    issue = (
        await Issue.objects.filter(
            id=cold_event.issue_id,
            project__organization__users=user_id,
        )
        .select_related("project")
        .afirst()
    )
    if not issue:
        return None
    cold_event.issue = issue
    return cold_event


async def get_alerts(
    user_id: int,
    organization_slug: str,
    project_slug: str | None = None,
) -> list[ProjectAlert]:
    if project_slug:
        qs = get_project_alert_queryset(user_id, organization_slug, project_slug)
    else:
        qs = ProjectAlert.objects.filter(
            project__organization__users=user_id,
            project__organization__slug=organization_slug,
        ).prefetch_related("alertrecipient_set")
    qs = _apply_compliance_filter(qs)
    return [a async for a in qs]


async def get_monitors(user_id: int, organization_slug: str) -> list[Monitor]:
    qs = get_monitor_queryset(user_id, organization_slug)
    qs = _apply_compliance_filter(qs)
    return [m async for m in qs]


async def _get_org_id(user_id: int, organization_slug: str) -> int:
    """Resolve org slug to ID, verifying user membership."""
    org_id = (
        await get_organization_for_user(user_id, organization_slug)
        .values_list("id", flat=True)
        .afirst()
    )
    if org_id is None:
        raise ValueError(f"Organization '{organization_slug}' not found")
    return org_id


async def get_logs(
    user_id: int,
    organization_slug: str,
    project_id: int | None = None,
    level: str | None = None,
    service: str | None = None,
    environment: str | None = None,
    query: str | None = None,
    trace_id: str | None = None,
    limit: int = 50,
) -> list[LogEventRow]:
    """Query logs from hot+cold storage via the existing combined query."""
    org_id = await _get_org_id(user_id, organization_slug)

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=7)

    level_values = parse_level_filters([level] if level else None)

    project_ids = [project_id] if project_id else None

    return await query_logs_combined(
        organization_id=org_id,
        start_dt=start_dt,
        end_dt=now,
        project_ids=project_ids,
        level_values=level_values,
        service=service,
        environment=environment,
        trace_id=trace_id,
        query=query,
        limit=min(limit, 100),
    )


async def get_log(
    user_id: int, organization_slug: str, log_id: str
) -> LogEventRow | None:
    """Get a single log event by ID."""
    from apps.logs.api import get_log_by_id

    org_id = await _get_org_id(user_id, organization_slug)
    try:
        uuid_val = UUID(log_id)
    except ValueError:
        return None
    return await get_log_by_id(org_id, uuid_val)


async def get_transaction_groups(
    user_id: int,
    organization_slug: str,
    project_ids: list[int] | None = None,
    query: str | None = None,
    sort: str = "-avg_duration",
    limit: int = 25,
) -> list[TransactionGroup]:
    """List transaction groups for an organization."""
    org_id = await _get_org_id(user_id, organization_slug)
    qs = TransactionGroup.objects.filter(organization_id=org_id)

    if project_ids:
        qs = qs.filter(project_id__in=project_ids)
    if query:
        qs = qs.filter(transaction__icontains=query)

    allowed_sorts = {
        "created",
        "-created",
        "avg_duration",
        "-avg_duration",
        "count",
        "-count",
    }
    if sort not in allowed_sorts:
        sort = "-avg_duration"

    qs = qs.order_by(sort)
    limit = min(limit, 100)
    return [tg async for tg in qs[:limit]]


async def get_transaction_group(
    user_id: int, organization_slug: str, group_id: int
) -> TransactionGroup | None:
    """Get a single transaction group by ID."""
    org_id = await _get_org_id(user_id, organization_slug)
    return await TransactionGroup.objects.filter(
        id=group_id, organization_id=org_id
    ).afirst()


async def get_transaction_spans(
    user_id: int,
    organization_slug: str,
    group_id: int,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> list[dict]:
    """Get span groups for a specific transaction (DuckDB cold storage)."""
    from apps.performance.cold_storage import query_span_groups_for_transaction

    org_id = await _get_org_id(user_id, organization_slug)

    # Verify user has access to this transaction group
    group = await TransactionGroup.objects.filter(
        id=group_id, organization_id=org_id
    ).afirst()
    if not group:
        return []

    now = datetime.now(timezone.utc)
    start = start_dt or (now - timedelta(days=7))
    end = end_dt or now

    return await sync_to_async(query_span_groups_for_transaction)(
        org_id=org_id,
        transaction_name=group.transaction,
        start_dt=start,
        end_dt=end,
    )


async def get_n_plus_one_patterns(
    user_id: int,
    organization_slug: str,
    project_ids: list[int] | None = None,
    op_filter: str | None = "db",
    threshold: float = 5.0,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Detect N+1 query patterns (DuckDB cold storage)."""
    from apps.performance.cold_storage import query_n_plus_one_patterns

    org_id = await _get_org_id(user_id, organization_slug)

    now = datetime.now(timezone.utc)
    start = start_dt or (now - timedelta(days=7))
    end = end_dt or now

    return await sync_to_async(query_n_plus_one_patterns)(
        org_id=org_id,
        project_ids=project_ids,
        start_dt=start,
        end_dt=end,
        op_filter=op_filter,
        threshold=threshold,
        limit=min(limit, 100),
    )


async def get_transaction_trend(
    user_id: int,
    organization_slug: str,
    group_id: int,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> list[dict]:
    """Get daily performance trend for a transaction group (DuckDB cold storage)."""
    from apps.performance.cold_storage import query_transaction_trend

    org_id = await _get_org_id(user_id, organization_slug)

    # Verify user has access and resolve transaction name
    group = await TransactionGroup.objects.filter(
        id=group_id, organization_id=org_id
    ).afirst()
    if not group:
        return []

    now = datetime.now(timezone.utc)
    start = start_dt or (now - timedelta(days=7))
    end = end_dt or now

    return await sync_to_async(query_transaction_trend)(
        org_id=org_id,
        transaction_name=group.transaction,
        start_dt=start,
        end_dt=end,
    )


async def get_span_groups(
    user_id: int,
    organization_slug: str,
    project_ids: list[int] | None = None,
    op_filter: str | None = None,
    sort: str = "-total_time",
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query span groups across the organization (DuckDB cold storage)."""
    from apps.performance.cold_storage import query_span_groups

    org_id = await _get_org_id(user_id, organization_slug)

    now = datetime.now(timezone.utc)
    start = start_dt or (now - timedelta(days=7))
    end = end_dt or now

    return await sync_to_async(query_span_groups)(
        org_id=org_id,
        project_ids=project_ids,
        start_dt=start,
        end_dt=end,
        op_filter=op_filter,
        sort=sort,
        limit=min(limit, 100),
    )


async def update_issue(
    user_id: int,
    issue_id: int,
    status: str,
    in_next_release: bool = False,
    in_release: str | None = None,
) -> Issue | None:
    """Update an issue's status (resolve, unresolve, ignore).

    Optionally associate a release when resolving.
    Returns the updated issue, or None if not found / no access.
    """
    qs = Issue.objects.filter(project__organization__users=user_id).select_related(
        "project__organization", "resolved_in_release"
    )
    qs = _apply_compliance_filter(qs)

    obj = await qs.filter(id=issue_id).afirst()
    if obj is None:
        return None

    new_status = EventStatus.from_string(status)
    if new_status is None:
        raise ValueError(f"Invalid status: {status!r}")
    obj.status = new_status
    update_fields = ["status"]

    if obj.status == EventStatus.RESOLVED:
        if in_release:
            release = await Release.objects.filter(
                version=in_release,
                organization_id=obj.project.organization_id,
            ).afirst()
            if release:
                obj.resolved_in_release = release
                update_fields.append("resolved_in_release_id")
        elif in_next_release:
            release = await (
                Release.objects.filter(projects=obj.project_id)
                .order_by("-created")
                .afirst()
            )
            if release:
                obj.resolved_in_release = release
                update_fields.append("resolved_in_release_id")
    else:
        obj.resolved_in_release = None
        update_fields.append("resolved_in_release_id")

    await obj.asave(update_fields=update_fields)
    return obj
