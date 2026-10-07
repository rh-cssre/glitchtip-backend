from collections import defaultdict
from datetime import timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDay
from django.db.models.query import QuerySet
from django.http import Http404, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Schema, Status
from ninja.errors import HttpError
from pydantic import model_validator

from apps.organizations_ext.models import Organization, OrganizationUser
from apps.projects.models import Project
from apps.releases.models import Release
from apps.releases.schema import CommitSchema
from apps.teams.models import Team
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import MissOnEmptyPagination
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    has_permission,
    org_scoped,
    org_scoped_list,
)

from ..constants import EventStatus
from ..models import Issue, IssueAggregate, IssueEvent, IssueHash, IssueIndex
from ..schema import (
    IssueDetailSchema,
    IssueSchema,
    IssueStatsResponse,
    IssueTagSchema,
    ShortIdLookupSchema,
    StatsDetailSchema,
)
from ..services import (
    IssueFilters,
    filter_issue_list,
    get_queryset,
    sort_options,
    with_issue_details,
)
from ..tasks import CHUNK_SIZE, delete_issue_task, update_issues_task
from ..utils import parse_short_id
from . import router

EventStatusEnum = StrEnum("EventStatusEnum", EventStatus.labels)


class StatusDetailsSchema(Schema):
    in_release: str | None = Field(default=None, validation_alias="inRelease")
    in_next_release: bool | None = Field(default=None, validation_alias="inNextRelease")


class UpdateIssueSchema(Schema):
    # sentry-cli sends "muted" for ignored and "resolvedInNextRelease"
    status: EventStatusEnum | Literal["muted", "resolvedInNextRelease"] | None = None
    status_details: StatusDetailsSchema | None = Field(
        default=None, validation_alias="statusDetails"
    )
    merge: int | None = None
    assigned_to: str | None = Field(default=None, validation_alias="assignedTo")

    @model_validator(mode="after")
    def normalize_status(self):
        if self.status == "muted":
            self.status = EventStatusEnum.ignored
        elif self.status == "resolvedInNextRelease":
            self.status = EventStatusEnum.resolved
            if self.status_details is None:
                self.status_details = StatusDetailsSchema()
            if not self.status_details.in_release:
                self.status_details.in_next_release = True
        return self


async def resolve_assignee(
    assigned_to: str | None, organization_id: int
) -> tuple[OrganizationUser | None, Team | None]:
    """Parse an assignedTo string into (org_user, team) objects.

    Accepted formats:
      - None or "" → unassign, returns (None, None)
      - "user:<id>" → active OrganizationUser by global User id
      - "team:<slug>" → team by slug
      - "<email>" → active OrganizationUser by User.email (bare string fallback)

    Only active (non-pending) members can be assigned — pending invites
    have no User yet. The team must belong to the organization. Raises
    ninja HttpError on failure.
    """
    if not assigned_to:
        return None, None

    if assigned_to.startswith("team:"):
        slug = assigned_to[len("team:") :]
        team = await Team.objects.filter(
            slug=slug, organization_id=organization_id
        ).afirst()
        if team is None:
            raise HttpError(404, "Team not found")
        return None, team

    if assigned_to.startswith("user:"):
        raw_id = assigned_to[len("user:") :]
        try:
            user_id = int(raw_id)
        except ValueError:
            raise HttpError(400, "Invalid assignedTo format")
        member_filter = {"user_id": user_id}
    else:
        member_filter = {"user__email": assigned_to}

    org_user = (
        await OrganizationUser.objects.select_related("user")
        .filter(
            organization_id=organization_id,
            user__isnull=False,
            **member_filter,
        )
        .afirst()
    )
    if org_user is None:
        raise HttpError(404, "User is not a member of this organization")
    return org_user, None


async def _aggregate_tags(issue: Issue, key: str | None):
    qs = issue.issuetag_set.filter(organization_id=issue.project.organization_id)
    if key:
        qs = qs.filter(tag_key__key=key)
    qs = (
        qs.values("tag_key__key", "tag_value__value")
        .annotate(total_count=Sum("count"))
        .order_by("-total_count")[:100000]
    )
    keys = {row["tag_key__key"] async for row in qs}
    return [
        {
            "topValues": [
                {
                    "name": group["tag_value__value"],
                    "value": group["tag_value__value"],
                    "count": group["total_count"],
                    "key": group["tag_key__key"],
                }
                for group in qs
                if group["tag_key__key"] == key
            ],
            "uniqueValues": len(
                [group for group in qs if group["tag_key__key"] == key]
            ),
            "key": key,
            "name": key,
            "totalValues": sum(
                [group["total_count"] for group in qs if group["tag_key__key"] == key]
            ),
        }
        for key in keys
    ]


@router.get(
    "/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_issue(request: AuthHttpRequest, issue_id: int):
    qs = await get_queryset(request.auth.user_id)
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        return await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def get_organization_issue(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
):
    qs = with_issue_details(qs, request.org_scope.org_id)
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        return await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()


@router.get(
    "/issues/{int:issue_id}/commits/",
    response=list[CommitSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_commits(request: AuthHttpRequest, issue_id: int):
    """Return commits from the release where this issue first appeared."""
    qs = await get_queryset(request.auth.user_id)
    try:
        issue = await qs.select_related("first_release").filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()
    if not issue.first_release_id:
        return []
    return issue.first_release.data.get("commits", [])


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/commits/",
    response=list[CommitSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def list_organization_issue_commits(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
):
    qs = with_issue_details(qs, request.org_scope.org_id)
    try:
        issue = await qs.select_related("first_release").filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()
    if not issue.first_release_id:
        return []
    return issue.first_release.data.get("commits", [])


@router.put(
    "/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
@has_permission(["event:write", "event:admin"])
async def update_issue(
    request: AuthHttpRequest,
    issue_id: int,
    payload: UpdateIssueSchema,
):
    qs = await get_queryset(request.auth.user_id)
    return await update_issue_status(qs, issue_id, payload)


@router.delete("/issues/{int:issue_id}/", response={204: None})
@has_permission(["event:write", "event:admin"])
async def delete_issue(request: AuthHttpRequest, issue_id: int):
    qs = await get_queryset(request.auth.user_id)
    result = await qs.filter(id=issue_id).aupdate(is_deleted=True)
    if not result:
        raise Http404()
    await delete_issue_task.aenqueue([issue_id])
    return Status(204, None)


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/",
    response={204: None},
)
@org_scoped(["event:write", "event:admin"], Issue)
async def delete_organization_issue(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
):
    result = await qs.filter(id=issue_id).aupdate(is_deleted=True)
    if not result:
        raise Http404()
    await delete_issue_task.aenqueue([issue_id])
    return Status(204, None)


@router.put(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
@org_scoped(["event:write", "event:admin"], Issue)
async def update_organization_issue(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
    payload: UpdateIssueSchema,
):
    qs = with_issue_details(qs, request.org_scope.org_id)
    return await update_issue_status(qs, issue_id, payload)


async def update_issue_status(qs: QuerySet, issue_id: int, payload: UpdateIssueSchema):
    """
    BC Gitlab integration
    """
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        obj = await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()
    update_fields: list[str] = []
    new_status: int | None = None

    if "status" in payload.model_fields_set and payload.status is not None:
        new_status = EventStatus.from_string(payload.status)
        # status lives on the IssueIndex leaf (written below);
        # resolved_in_release stays on Issue.
        if new_status == EventStatus.RESOLVED and payload.status_details:
            if payload.status_details.in_release:
                release = await Release.objects.filter(
                    version=payload.status_details.in_release,
                    organization_id=obj.project.organization_id,
                ).afirst()
                if release:
                    obj.resolved_in_release = release
                    update_fields.append("resolved_in_release_id")
            elif payload.status_details.in_next_release:
                release = await (
                    Release.objects.filter(
                        projects=obj.project_id,
                    )
                    .order_by("-created")
                    .afirst()
                )
                if release:
                    obj.resolved_in_release = release
                    update_fields.append("resolved_in_release_id")
        elif new_status != EventStatus.RESOLVED:
            obj.resolved_in_release = None
            update_fields.append("resolved_in_release_id")

    if "assigned_to" in payload.model_fields_set:
        org_user, team = await resolve_assignee(
            payload.assigned_to, obj.project.organization_id
        )
        obj.assigned_to_org_user = org_user
        obj.assigned_to_team = team
        update_fields.extend(["assigned_to_org_user_id", "assigned_to_team_id"])

    if update_fields:
        await obj.asave(update_fields=update_fields)
    if new_status is not None:
        # Include organization_id (the partition key) so the update prunes to a
        # single hash partition instead of scanning all of them.
        await IssueIndex.objects.filter(
            issue_id=obj.id, organization_id=obj.project.organization_id
        ).aupdate(status=new_status)
        # Reflect the change on the already-loaded leaf so the serialized
        # response (which reads issue.status -> index.status) is current.
        obj.index.status = new_status
    return obj


async def get_short_id_hit(
    organization_id: int, query: str, project_slug: str | None = None
) -> int | None:
    """Issue id when a search query is a short ID such as ``PROJECT-1A``"""
    parsed = parse_short_id(query)
    if parsed is None:
        return None
    slug, short_id = parsed
    if project_slug is not None and slug != project_slug:
        return None
    # Callers filter their permission-scoped queryset by the returned id
    return (
        await Issue.objects.filter(
            project__organization_id=organization_id,
            project__slug=slug,
            short_id=short_id,
            is_deleted=False,
        )
        .values_list("id", flat=True)
        .afirst()
    )


@router.get(
    "organizations/{slug:organization_slug}/shortids/{short_id}/",
    response=ShortIdLookupSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_issue_by_short_id(
    request: AuthHttpRequest, organization_slug: str, short_id: str
):
    """Resolve a short ID such as ``PROJECT-1A`` to its issue"""
    parsed = parse_short_id(short_id)
    if parsed is None:
        raise Http404()
    project_slug, number = parsed
    qs = await get_queryset(request.auth.user_id, organization_slug=organization_slug)
    try:
        issue = await qs.filter(
            project__slug=project_slug, short_id=number, is_deleted=False
        ).aget()
    except Issue.DoesNotExist:
        raise Http404()
    request.matching_event_id = None
    return {
        "organizationSlug": organization_slug,
        "projectSlug": issue.project.slug,
        "groupId": str(issue.id),
        "group": issue,
        "shortId": issue.short_id_display,
    }


@router.get(
    "organizations/{slug:organization_id_or_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
@org_scoped_list(
    ["event:read", "event:write", "event:admin"],
    Issue,
    pagination=MissOnEmptyPagination,
)
async def list_issues(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    filters: Query[IssueFilters],
    sort: sort_options = "-last_seen",
    short_id_lookup: str | None = Query(None, alias="shortIdLookup"),
):
    qs = with_issue_details(Issue.objects.all(), request.org_scope.org_id)
    event_id: UUID | None = None
    if filters.query:
        if short_id_lookup == "1" and (
            issue_id := await get_short_id_hit(request.org_scope.org_id, filters.query)
        ):
            # A short ID names one issue, so the other filters don't apply
            response["X-Sentry-Direct-Hit"] = "1"
            return filter_issue_list(qs.filter(id=issue_id), {}, sort)
        try:
            event_id = UUID(filters.query)
        except ValueError:
            event_id = None
        if event_id is not None:
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
    # Both text search (index join) and client-SDK UUIDv4 event-id lookups
    # scan org-partitioned tables; the org id lets Postgres prune them.
    return filter_issue_list(
        qs, filters, sort, event_id, organization_id=request.org_scope.org_id
    )


@router.delete(
    "organizations/{slug:organization_id_or_slug}/issues/", response=UpdateIssueSchema
)
@org_scoped(["event:write", "event:admin"], Issue)
async def delete_issues(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    filters: Query[IssueFilters],
):
    qs = filter_issue_list(
        qs.filter(index__organization_id=request.org_scope.org_id), filters
    )
    # Ids first: the gated qs excludes deleted issues, so it can't find them
    # again after the update.
    issue_ids = [issue_id async for issue_id in qs.values_list("id", flat=True)]
    if not issue_ids and (error := await request.org_scope.explain_miss()):
        raise error
    await Issue.objects.filter(id__in=issue_ids).aupdate(is_deleted=True)
    await delete_issue_task.aenqueue(issue_ids)
    return {"status": "resolved"}


@router.put(
    "organizations/{slug:organization_id_or_slug}/issues/", response=UpdateIssueSchema
)
@org_scoped(["event:write", "event:admin"], Issue)
async def update_issues(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    filters: Query[IssueFilters],
    payload: UpdateIssueSchema,
):
    return await bulk_update_issues(request, qs, filters, payload)


@router.put(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/issues/",
    response=UpdateIssueSchema,
)
@org_scoped(["event:write", "event:admin"], Issue)
async def update_project_issues(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    project_slug: str,
    filters: Query[IssueFilters],
    payload: UpdateIssueSchema,
    status_filter: str | None = Query(None, alias="status"),
):
    """
    Bulk update a project's issues, selected by repeated ``id`` parameters.
    Without ``id`` only a status change is allowed; it applies to the issues
    with the ``status`` given in the query string, or to unresolved issues.
    """
    project_id = (
        await request.org_scope.apply(Project.objects.filter(slug=project_slug))
        .values_list("id", flat=True)
        .afirst()
    )
    if project_id is None:
        raise await request.org_scope.explain_miss() or Http404()
    filters.project__in = [project_id]

    if not filters.id__in and payload.model_fields_set - {"status", "status_details"}:
        raise HttpError(400, "Only a status change may omit id")
    if status_filter is not None:
        if status_filter == "muted":
            status_filter = "ignored"
        if status_filter not in EventStatus.labels:
            raise HttpError(400, f"Invalid status: {status_filter}")
        # Written into the query so the background batches apply it too
        filters.query = f"is:{status_filter} {filters.query or ''}".strip()
    elif not filters.id__in and not (filters.query or "").strip():
        filters.query = "is:unresolved"

    return await bulk_update_issues(request, qs, filters, payload)


async def bulk_update_issues(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    filters: IssueFilters,
    payload: UpdateIssueSchema,
):
    user_id = request.auth.user_id
    organization_id = request.org_scope.org_id
    # The gated qs, not get_queryset(): its comment count annotation adds a
    # GROUP BY that turns the id selections below into a scan of every org's
    # issues, and DISTINCT does the same. Tag filters may repeat an issue (one
    # IssueTag row per day); the selections below tolerate duplicate ids.
    qs = filter_issue_list(qs.filter(index__organization_id=organization_id), filters)

    # Mirrors update_issue_status: resolving may pin a release, any other
    # status clears it.
    release_update: dict[str, int | None] = {}
    if payload.status and payload.status != EventStatus.RESOLVED.label:
        release_update = {"resolved_in_release_id": None}
    elif payload.status and payload.status_details:
        details = payload.status_details
        release_qs = None
        if details.in_release:
            release_qs = Release.objects.filter(
                version=details.in_release, organization_id=organization_id
            )
        elif details.in_next_release:
            # "Next" is per project
            if len(filters.project__in or []) != 1:
                raise HttpError(400, "inNextRelease needs a single project")
            release_qs = Release.objects.filter(
                projects=filters.project__in[0], organization_id=organization_id
            ).order_by("-created")
        if release_qs is not None:
            release_id = await release_qs.values_list("id", flat=True).afirst()
            if release_id:
                release_update = {"resolved_in_release_id": release_id}

    merge_issue = None
    if payload.merge:
        qs = qs.filter(is_deleted=False)
        merge_issue = await qs.order_by("-id").afirst()
        if (
            merge_issue
            and await qs.exclude(project_id=merge_issue.project_id).aexists()
        ):
            raise HttpError(400, "Cannot merge issues from different projects")

    # Freeze the set of issues to update to avoid race conditions with new issues
    max_id = (
        merge_issue.id
        if merge_issue
        else await qs.order_by("-id").values_list("id", flat=True).afirst()
    )
    if not max_id:
        if error := await request.org_scope.explain_miss():
            raise error
        return payload

    qs = qs.filter(id__lte=max_id)

    # Process a limited batch immediately for UI responsiveness
    limit = 50
    updated_ids = [i async for i in qs.values_list("id", flat=True)[:limit]]

    should_enqueue = len(updated_ids) == limit
    update_params = payload.model_dump(mode="json") | release_update
    if merge_issue:
        update_params["merge"] = merge_issue.id

    if payload.status:
        # status lives on the IssueIndex leaf; the partition key prunes
        # the update to a single hash partition.
        await IssueIndex.objects.filter(
            issue_id__in=updated_ids, organization_id=organization_id
        ).aupdate(status=EventStatus.from_string(payload.status))
    if release_update:
        await Issue.objects.filter(id__in=updated_ids).aupdate(**release_update)

    if "assigned_to" in payload.model_fields_set:
        assignee_org_user, assignee_team = await resolve_assignee(
            payload.assigned_to, organization_id
        )
        update_params["assigned_to_org_user_id"] = (
            assignee_org_user.id if assignee_org_user else None
        )
        update_params["assigned_to_team_id"] = (
            assignee_team.id if assignee_team else None
        )
        await Issue.objects.filter(id__in=updated_ids).aupdate(
            assigned_to_org_user_id=update_params["assigned_to_org_user_id"],
            assigned_to_team_id=update_params["assigned_to_team_id"],
        )

    needs_drain = False
    if merge_issue:
        remove_qs = Issue.objects.filter(
            id__in=updated_ids, project_id=merge_issue.project_id
        ).exclude(id=merge_issue.id)
        await IssueHash.objects.filter(issue__in=remove_qs).aupdate(issue=merge_issue)

        event_ids = [
            event_id
            async for event_id in IssueEvent.objects.filter(
                issue__in=remove_qs,
                organization_id=organization_id,
            ).values_list("id", flat=True)[:CHUNK_SIZE]
        ]
        moved_event_count = await IssueEvent.objects.filter(
            id__in=event_ids,
            issue__in=remove_qs,
            organization_id=organization_id,
        ).aupdate(issue=merge_issue)
        if moved_event_count:
            await IssueIndex.objects.filter(
                issue_id=merge_issue.id,
                organization_id=organization_id,
            ).aupdate(count=F("count") + moved_event_count)

        needs_drain = len(event_ids) == CHUNK_SIZE
        if not needs_drain:
            await remove_qs.aupdate(is_deleted=True)

    if should_enqueue or needs_drain:
        await update_issues_task.aenqueue(
            # The task re-resolves the organization by slug, so it needs the
            # real one; the path may carry an id.
            organization_slug=await Organization.objects.filter(id=organization_id)
            .values_list("slug", flat=True)
            .aget(),
            user_id=user_id,
            filter_params=filters.model_dump(by_alias=True, mode="json"),
            update_params=update_params,
            exclude_ids=updated_ids,
            max_id=max_id,
        )

    return payload


@router.get(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
@org_scoped_list(
    ["event:read", "event:write", "event:admin"],
    Issue,
    pagination=MissOnEmptyPagination,
)
async def list_project_issues(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
    filters: Query[IssueFilters],
    sort: sort_options = "-last_seen",
    short_id_lookup: str | None = Query(None, alias="shortIdLookup"),
):
    qs = with_issue_details(
        Issue.objects.filter(project__slug=project_slug), request.org_scope.org_id
    )
    event_id: UUID | None = None
    if filters.query:
        if short_id_lookup == "1" and (
            issue_id := await get_short_id_hit(
                request.org_scope.org_id, filters.query, project_slug
            )
        ):
            # A short ID names one issue, so the other filters don't apply
            response["X-Sentry-Direct-Hit"] = "1"
            return filter_issue_list(qs.filter(id=issue_id), {}, sort)
        try:
            event_id = UUID(filters.query)
        except ValueError:
            event_id = None
        if event_id is not None:
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
    # Both text search (index join) and client-SDK UUIDv4 event-id lookups
    # scan org-partitioned tables; the org id lets Postgres prune them.
    return filter_issue_list(
        qs, filters, sort, event_id, organization_id=request.org_scope.org_id
    )


@router.get(
    "/issues/{int:issue_id}/tags/", response=list[IssueTagSchema], by_alias=True
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_tags(
    request: AuthHttpRequest, issue_id: int, key: str | None = None
):
    qs = await get_queryset(request.auth.user_id)
    try:
        issue = await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()

    return await _aggregate_tags(issue, key)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/tags/",
    response=list[IssueTagSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def list_organization_issue_tags(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
    key: str | None = None,
):
    qs = with_issue_details(qs, request.org_scope.org_id)
    try:
        issue = await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()

    return await _aggregate_tags(issue, key)


class IssueStatsFilters(Schema):
    groups: list[int]
    statsPeriod: Literal["14d", "24h"] = "24h"


@router.get(
    "organizations/{slug:organization_id_or_slug}/issues-stats/",
    response=list[IssueStatsResponse],
    summary="Retrieve Statistics for a Set of Issues",
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def issue_stats(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    filters: Query[IssueStatsFilters],
):
    """
    Retrieves aggregated statistics for a given list of issue groups.

    This endpoint returns data for the last 24 hours, formatted as a series of
    [timestamp, count] pairs.
    """
    issues_qs = qs.filter(id__in=filters.groups).select_related("index")[
        :200
    ]  # Sanity limit; leaf for count/last_seen

    issue_list = [issue async for issue in issues_qs]
    issue_ids = [issue.id for issue in issue_list]

    if not issue_ids:
        if error := await request.org_scope.explain_miss():
            raise error
        return []

    is_24h = filters.statsPeriod == "24h"
    stats_map = defaultdict(list)

    if is_24h:
        # --- 24-Hour Period: Fetch hourly data ---
        start_date = timezone.now() - timedelta(hours=24)

        # Fetch pre-aggregated hourly stats from the last 24 hours.
        stats_qs = IssueAggregate.objects.filter(
            issue_id__in=issue_ids, date__gte=start_date
        ).values("issue_id", "date", "count")

        stats_list = [stat async for stat in stats_qs]

        # Group the hourly stats by issue_id.
        for stat in stats_list:
            timestamp = int(stat["date"].timestamp())
            stats_map[stat["issue_id"]].append([timestamp, stat["count"]])

        # Define a function to return the correct stats argument for the response.
        def get_stats_data(issue_id):
            return {"stats_24h": stats_map.get(issue_id, [])}

    else:
        # --- 14-Day Period: Fetch and group data by day ---
        start_date = timezone.now() - timedelta(days=14)

        # Fetch stats and aggregate them by day.
        daily_stats_qs = (
            IssueAggregate.objects.filter(issue_id__in=issue_ids, date__gte=start_date)
            .annotate(day=TruncDay("date"))
            .values("issue_id", "day")
            .annotate(daily_count=Sum("count"))
            .order_by("day")
        )

        daily_stats_list = [stat async for stat in daily_stats_qs]

        # Group the daily stats by issue_id.
        for stat in daily_stats_list:
            timestamp = int(stat["day"].timestamp())
            stats_map[stat["issue_id"]].append([timestamp, stat["daily_count"]])

        # Define a function to return the correct stats argument for the response.
        def get_stats_data(issue_id):
            return {"stats_14d": stats_map.get(issue_id, [])}

    return [
        IssueStatsResponse(
            id=str(issue.id),
            count=str(issue.count),
            user_count=issue.count,
            first_seen=issue.first_seen.isoformat(),
            last_seen=issue.last_seen.isoformat(),
            is_unhandled=issue.metadata.get("unhandled", False),
            stats=StatsDetailSchema(**get_stats_data(issue.id)),
        )
        for issue in issue_list
    ]
