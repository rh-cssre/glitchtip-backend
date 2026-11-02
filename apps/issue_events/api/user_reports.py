from django.http import HttpResponse
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    has_permission,
    org_scoped_list,
)

from ..models import UserReport
from ..schema import UserReportSchema
from . import router


@router.get(
    "/issues/{int:issue_id}/user-reports/",
    response=list[UserReportSchema],
    by_alias=True,
)
@paginate
@has_permission(["event:read", "event:write", "event:admin"])
async def list_user_reports(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    user_id = request.auth.user_id
    return UserReport.objects.filter(
        project__organization__users=user_id, issue__id=issue_id
    )


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/user-reports/",
    response=list[UserReportSchema],
    by_alias=True,
)
@org_scoped_list(["event:read", "event:write", "event:admin"], UserReport)
async def list_organization_user_reports(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    issue_id: int,
):
    return UserReport.objects.filter(issue_id=issue_id)
