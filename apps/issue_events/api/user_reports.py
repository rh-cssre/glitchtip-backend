from typing import List

from django.http import HttpResponse

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate

from ..models import UserReport
from ..schema import UserReportSchema
from . import router


@router.get(
    "/issues/{int:issue_id}/user-reports",
    response=List[UserReportSchema],
    by_alias=True,
)
@paginate
async def list_user_reports(request: AuthHttpRequest, response: HttpResponse, issue_id: int):
    user_id = request.auth
    return UserReport.objects.filter(
        project__organization__users=user_id,
        issue__id=issue_id
    )
