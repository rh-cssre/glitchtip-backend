from typing import List, Literal

from django.http import HttpResponse
from ninja import Schema
from ninja.errors import HttpError
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from ..models import Comment, Issue
from ..schema import CommentSchema
from . import router


def get_queryset(request: AuthHttpRequest, issue_id: int):
    user_id = request.auth.user_id
    return Comment.objects.select_related("user").filter(
        issue__project__organization__users=user_id, issue__id=issue_id
    )


@router.get(
    "/issues/{int:issue_id}/comments/",
    response=List[CommentSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:admin"])
@paginate
async def list_comments(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    return get_queryset(request, issue_id)


class PostCommentSchema(Schema):
    data: dict[Literal["text"], str]


@router.post(
    "/issues/{int:issue_id}/comments/",
    response={201: CommentSchema},
    by_alias=True,
)
@has_permission(["event:write", "event:admin"])
async def add_comment(
    request: AuthHttpRequest,
    issue_id: int,
    payload: PostCommentSchema,
):
    try:
        issue = await Issue.objects.aget(
            id=issue_id, project__organization__users=request.auth.user_id
        )
    except Issue.DoesNotExist:
        raise HttpError(400, "Issue does not exist")

    user_id = request.auth.user_id
    comment = await Comment.objects.acreate(
        text=payload.data["text"],
        issue=issue,
        user_id=user_id,
    )

    return 201, await Comment.objects.select_related("user").aget(id=comment.id)


@router.put(
    "/issues/{int:issue_id}/comments/{int:comment_id}/",
    response=CommentSchema,
    by_alias=True,
)
@has_permission(["event:write", "event:admin"])
async def update_comment(
    request: AuthHttpRequest,
    issue_id: int,
    comment_id: int,
    payload: PostCommentSchema,
):
    try:
        comment = await get_queryset(request, issue_id).aget(id=comment_id)
    except Comment.DoesNotExist:
        raise HttpError(400, "Comment does not exist")

    comment.text = payload.data["text"]
    await comment.asave()

    return comment


@router.delete(
    "/issues/{int:issue_id}/comments/{int:comment_id}/",
    response={204: None},
    by_alias=True,
)
@has_permission(["event:admin"])
async def delete_comment(
    request: AuthHttpRequest,
    issue_id: int,
    comment_id: int,
):
    try:
        comment = await get_queryset(request, issue_id).aget(id=comment_id)
    except Comment.DoesNotExist:
        raise HttpError(400, "Comment does not exist")
    await comment.adelete()

    return 204, None
