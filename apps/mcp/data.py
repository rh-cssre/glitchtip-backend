from uuid import UUID

from django.db.models import Q, QuerySet

from apps.alerts.api import get_project_alert_queryset
from apps.alerts.models import ProjectAlert
from apps.issue_events.models import Issue, IssueEvent
from apps.issue_events.services import filter_issue_list
from apps.issue_events.services import get_queryset as get_issues_qs
from apps.organizations_ext.models import Organization
from apps.organizations_ext.queryset_utils import get_organizations_queryset
from apps.projects.api import get_projects_queryset
from apps.projects.models import Project
from apps.uptime.api import get_monitor_queryset
from apps.uptime.models import Monitor
from glitchtip.api.pagination import AsyncLinkHeaderPagination


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
        "project"
    )
    qs = _apply_compliance_filter(qs)
    return await qs.filter(id=issue_id).afirst()


async def get_latest_event(user_id: int, issue_id: int) -> IssueEvent | None:
    issue = await Issue.objects.filter(
        id=issue_id, project__organization__users=user_id
    ).afirst()
    if not issue:
        return None
    qs = IssueEvent.objects.filter(
        issue_id=issue_id,
        organization__users=user_id,
    ).order_by("-id")
    qs = _apply_compliance_filter(qs)
    return await qs.afirst()


async def get_event(user_id: int, event_id: str) -> IssueEvent | None:
    """Look up an event by UUID. Accepts either the server-generated UUIDv7 id
    or the client-provided Sentry SDK event_id (UUIDv4).
    Includes related issue and project for full context."""
    try:
        uuid_val = UUID(event_id)
    except ValueError:
        return None
    qs = IssueEvent.objects.filter(
        organization__users=user_id,
    ).select_related("issue", "issue__project")
    qs = _apply_compliance_filter(qs)
    return await qs.filter(Q(id=uuid_val) | Q(event_id=uuid_val)).afirst()


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
