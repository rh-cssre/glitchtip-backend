from typing import List
from django.http import HttpResponse

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import apaginate

from ..models import UserReport
from ..schema import UserReportSchema
from . import router


def get_queryset(request: AuthHttpRequest):
    user_id = request.auth
    return UserReport.objects.filter(project__organization__users=user_id)

@router.get(
    "/issues/{int:issue_id}/user-reports",
    response=List[UserReportSchema],
    by_alias=True,
)
@apaginate
async def list_user_reports(request: AuthHttpRequest, response: HttpResponse, issue_id: int):
    return get_queryset(request).filter(issue__id=issue_id)
