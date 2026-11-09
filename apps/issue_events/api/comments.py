from typing import Literal

from django.db.models import QuerySet
from django.http import Http404, HttpResponse
from ninja import Schema, Status
from ninja.errors import HttpError
from ninja.pagination import paginate

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.queryset_utils import annotate_actor_role
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    has_permission,
    org_scoped,
    org_scoped_list,
)

from ..models import Comment, Issue
from ..schema import CommentSchema
from . import router


def get_queryset(request: AuthHttpRequest, issue_id: int):
    return Comment.objects.select_related("user").filter(
        issue__project__organization__users=request.auth.user_id, issue__id=issue_id
    )


@router.get(
    "/issues/{int:issue_id}/comments/",
    response=list[CommentSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:admin"])
@paginate
async def list_comments(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    return get_queryset(request, issue_id)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/comments/",
    response=list[CommentSchema],
    by_alias=True,
)
@org_scoped_list(["event:read", "event:admin"], Comment)
async def list_organization_comments(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    issue_id: int,
    organization_id_or_slug: str,
):
    return Comment.objects.select_related("user").filter(issue_id=issue_id)


class PostCommentSchema(Schema):
    data: dict[Literal["text"], str]


async def get_comment_for_change(
    request: OrgScopedHttpRequest, qs: QuerySet[Comment], issue_id: int, comment_id: int
) -> Comment:
    """The comment, if the caller wrote it or is a manager or owner."""
    user_id = request.auth.user_id
    try:
        comment = await annotate_actor_role(
            qs, user_id, "issue__project__organization_id"
        ).aget(issue_id=issue_id, id=comment_id)
    except Comment.DoesNotExist:
        raise Http404()
    if comment.user_id != user_id and comment.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(
            403, "Changing another member's comment requires the manager role or above."
        )
    return comment


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

    return Status(201, await Comment.objects.select_related("user").aget(id=comment.id))


@router.post(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/comments/",
    response={201: CommentSchema},
    by_alias=True,
)
@org_scoped(["event:write", "event:admin"], Issue)
async def add_organization_comment(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
    payload: PostCommentSchema,
):
    try:
        issue = await qs.aget(id=issue_id)
    except Issue.DoesNotExist:
        raise Http404()

    user_id = request.auth.user_id
    comment = await Comment.objects.acreate(
        text=payload.data["text"],
        issue=issue,
        user_id=user_id,
    )

    return Status(201, await Comment.objects.select_related("user").aget(id=comment.id))


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


@router.put(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/comments/{int:comment_id}/",
    response=CommentSchema,
    by_alias=True,
)
@org_scoped(["event:write", "event:admin"], Comment)
async def update_organization_comment(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Comment],
    organization_id_or_slug: str,
    issue_id: int,
    comment_id: int,
    payload: PostCommentSchema,
):
    comment = await get_comment_for_change(
        request, qs.select_related("user"), issue_id, comment_id
    )
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

    return Status(204, None)


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/comments/{int:comment_id}/",
    response={204: None},
    by_alias=True,
)
@org_scoped(["event:admin"], Comment)
async def delete_organization_comment(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Comment],
    organization_id_or_slug: str,
    issue_id: int,
    comment_id: int,
):
    comment = await get_comment_for_change(request, qs, issue_id, comment_id)
    await comment.adelete()

    return Status(204, None)
