from django.http import Http404

from glitchtip.api.authentication import AuthHttpRequest

from ..models import Issue
from ..schema import IssueSchema
from . import router


def get_queryset(request: AuthHttpRequest):
    user_id = request.auth
    return Issue.objects.filter(project__organization__users=user_id)


@router.get(
    "/issues/{int:issue_id}/",
    response=IssueSchema,
    by_alias=True,
)
async def get_issue(request: AuthHttpRequest, issue_id: int):
    qs = get_queryset(request)
    try:
        return await qs.filter(id=issue_id).select_related('project').aget()
    except Issue.DoesNotExist:
        raise Http404()
