from typing import List

from django.http import HttpResponse
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from ..models import UserReport
from ..schema import UserReportSchema
from . import router


@router.get(
    "/issues/{int:issue_id}/user-reports/",
    response=List[UserReportSchema],
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
