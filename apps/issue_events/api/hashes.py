from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja.pagination import paginate

from apps.issue_events.models import IssueHash
from apps.issue_events.schema import IssueHashSchema
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from . import router


@router.get(
    "/organizations/{slug:organization_slug}/issues/{int:issue_id}/hashes/",
    response=list[IssueHashSchema],
    by_alias=True,
)
@has_permission(["event:read"])
@paginate
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
