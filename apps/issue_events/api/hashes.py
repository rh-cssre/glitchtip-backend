from uuid import UUID

from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Query, Schema
from ninja.pagination import paginate

from apps.issue_events.models import IssueEvent, IssueHash
from apps.issue_events.schema import IssueHashSchema
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import AsyncLinkHeaderPagination
from glitchtip.api.permissions import has_permission

from . import router


class IssueHashPagination(AsyncLinkHeaderPagination):
    async def get_results(self, queryset, cursor, limit):
        result = await super().get_results(queryset, cursor, limit)
        # There is no foreign key connecting a hash to an event.
        # So we must query once per hash
        for issue_hash in result:
            issue_hash.latest_event = (
                await IssueEvent.objects.filter(
                    hashes__contains=[issue_hash.value.hex],
                    issue__project_id=issue_hash.project_id,
                )
                .select_related("issue")
                .order_by("-received")
                .afirst()
            )
        return result


@router.get(
    "/organizations/{slug:organization_slug}/issues/{int:issue_id}/hashes/",
    response=list[IssueHashSchema],
    by_alias=True,
)
@has_permission(["event:read"])
@paginate(IssueHashPagination)
async def list_issue_hashes(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    issue_id: int,
):
    organization = await aget_object_or_404(
        Organization, users=request.auth.user_id, slug=organization_slug
    )
    return IssueHash.objects.filter(
        issue_id=issue_id, issue__project__organization=organization
    ).order_by("value")


class IssueHashQuerySchema(Schema):
    id: list[UUID]


@router.delete(
    "/organizations/{slug:organization_slug}/issues/{int:issue_id}/hashes/",
    response={202: None},
)
@has_permission(["event:admin"])
async def delete_hash(
    request: AuthHttpRequest,
    organization_slug: str,
    issue_id: int,
    query: Query[IssueHashQuerySchema],
):
    organization = await aget_object_or_404(
        Organization, users=request.auth.user_id, slug=organization_slug
    )
    await IssueHash.objects.filter(
        value__in=query.id,
        issue_id=issue_id,
        issue__project__organization=organization,
    ).adelete()
    return 202, None
