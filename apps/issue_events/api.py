import uuid
from typing import Optional

from ninja import Router
from django.http import HttpRequest

from .schema import IssueEventSchema
from .models import IssueEvent


router = Router()


@router.get(
    "/issues/{int:issue_id}/events/", response=list[IssueEventSchema], by_alias=True
)
async def issue_event_list(request: HttpRequest, issue_id: int):
    user_id = request.session.get("_auth_user_id")
    qs = IssueEvent.objects.filter(
        issue__project__organization__users=user_id, issue_id=issue_id
    )
    return [obj async for obj in qs]


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventSchema,
    by_alias=True,
)
async def issue_event_retrieve(
    request: HttpRequest, issue_id: int, event_id: uuid.UUID
):
    return await IssueEvent.objects.afirst()


@router.get("/issues/{int:issue_id}/events/latest/")
async def issue_event_latest(request: HttpRequest, issue_id: int):
    return ""
