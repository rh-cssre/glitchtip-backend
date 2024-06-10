from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError

from apps.shared.types import MeID
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate

from .models import User
from .schema import UserIn, UserSchema

router = Router()


"""
Sentry OSS does not document any of these, but they exist
GET /users/
GET /users/<me_id>/
DELETE /users/<me_id>/
PUT /users/<me_id>/
GET /organizations/burke-software/users/ (Not implemented)
"""


def get_user_queryset(user_id: int, add_details=False):
    qs = User.objects.filter(id=user_id)
    if add_details:
        qs = qs.prefetch_related("socialaccount_set")
    return qs


@router.get("/users/", response=list[UserSchema], by_alias=True)
@paginate
async def list_users(request: AuthHttpRequest, response: HttpResponse):
    """
    Exists in Sentry OSS, unsure what the use case is
    We make it only list the current user
    """
    return get_user_queryset(user_id=request.auth.user_id, add_details=True)


@router.get("/users/{slug:user_id}/", response=UserSchema, by_alias=True)
async def get_user(request: AuthHttpRequest, user_id: MeID):
    user_id = request.auth.user_id
    return await aget_object_or_404(get_user_queryset(user_id, add_details=True))


@router.delete("/users/{slug:user_id}/", response={204: None})
async def delete_user(request: AuthHttpRequest, user_id: MeID):
    # Can only delete self
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id
    queryset = get_user_queryset(user_id=user_id)
    result, _ = await queryset.filter(
        organizations_ext_organizationuser__organizationowner__isnull=True
    ).adelete()
    if result:
        return 204, None
    if await queryset.aexists():
        raise HttpError(
            400,
            "User is organization owner. Delete organization or transfer ownership first.",
        )
    raise Http404


@router.put(
    "/users/{slug:user_id}/",
    response=UserSchema,
    by_alias=True,
)
async def update_user(request: AuthHttpRequest, user_id: MeID, payload: UserIn):
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id
    user = await aget_object_or_404(get_user_queryset(user_id, add_details=True))

    for attr, value in payload.dict().items():
        setattr(user, attr, value)
    await user.asave()

    return user
