from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpResponse
from ninja import Query, Schema, Status

from apps.issue_events.models import IssueEvent, IssueHash
from apps.issue_events.schema import IssueHashSchema
from glitchtip.api.pagination import AsyncLinkHeaderPagination, MissOnEmptyMixin
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    org_scoped,
    org_scoped_list,
)

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
                .order_by("-id")  # UUIDv7 ordering = time ordering
                .afirst()
            )
        return result


class GatedIssueHashPagination(MissOnEmptyMixin, IssueHashPagination):
    pass


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/hashes/",
    response=list[IssueHashSchema],
    by_alias=True,
)
@org_scoped_list(["event:read"], IssueHash, pagination=GatedIssueHashPagination)
async def list_issue_hashes(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    issue_id: int,
):
    return IssueHash.objects.filter(issue_id=issue_id).order_by("value")


class IssueHashQuerySchema(Schema):
    id: list[UUID]


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/hashes/",
    response={202: None},
)
@org_scoped(["event:admin"], IssueHash)
async def delete_hash(
    request: OrgScopedHttpRequest,
    qs: QuerySet[IssueHash],
    organization_id_or_slug: str,
    issue_id: int,
    query: Query[IssueHashQuerySchema],
):
    deleted, _ = await qs.filter(value__in=query.id, issue_id=issue_id).adelete()
    # Nothing to delete is a 202, unless the caller isn't a member.
    if not deleted and (error := await request.org_scope.explain_miss()):
        raise error
    return Status(202, None)
