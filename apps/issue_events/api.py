import uuid
from typing import Optional

from ninja import Router

from glitchtip.api.authentication import AuthHttpRequest
from .schema import IssueEventSchema
from .models import IssueEvent


router = Router()


def get_queryset(user_id: str, issue_id: int):
    return IssueEvent.objects.filter(
        issue__project__organization__users=user_id, issue_id=issue_id
    )


@router.get(
    "/issues/{int:issue_id}/events/", response=list[IssueEventSchema], by_alias=True
)
async def issue_event_list(request: AuthHttpRequest, issue_id: int):
    user_id = request.auth
    if user_id:
        qs = get_queryset(user_id, issue_id)
        return [obj async for obj in qs]
    return []


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventSchema,
    by_alias=True,
)
async def issue_event_retrieve(
    request: AuthHttpRequest, issue_id: int, event_id: uuid.UUID
):
    user_id = request.auth
    if user_id:
        qs = get_queryset(user_id, issue_id)
        return await qs.afirst()
    return None


@router.get("/issues/{int:issue_id}/events/latest/")
async def issue_event_latest(request: AuthHttpRequest, issue_id: int):
    return ""
