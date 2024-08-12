from allauth.account.models import EmailAddress
from asgiref.sync import sync_to_async
from django.db.utils import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import paginate

from apps.shared.types import MeID
from glitchtip.api.authentication import AuthHttpRequest

from .models import User
from .schema import (
    EmailAddressIn,
    EmailAddressSchema,
    UserIn,
    UserNotificationsSchema,
    UserSchema,
)

router = Router()


"""
Sentry OSS does not document any of these, but they exist
GET /users/
GET /users/<me_id>/
DELETE /users/<me_id>/
PUT /users/<me_id>/
GET /organizations/burke-software/users/ (Not implemented)
GET /users/<me_id>/emails/
POST /users/<me_id>/emails/
PUT /users/<me_id>/emails/ (Set as primary)
DELETE /users/<me_id>/emails/
GET /users/<me_id>/notifications/
PUT /users/<me_id>/notifications/
"""


def get_user_queryset(user_id: int, add_details=False):
    qs = User.objects.filter(id=user_id)
    if add_details:
        qs = qs.prefetch_related("socialaccount_set")
    return qs


def get_email_queryset(user_id: int, verified: bool = None):
    qs = EmailAddress.objects.filter(user_id=user_id)
    if verified:
        qs = qs.filter(verified=verified)
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


@router.get(
    "/users/{slug:user_id}/emails/", response=list[EmailAddressSchema], by_alias=True
)
async def list_emails(request: AuthHttpRequest, user_id: MeID):
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id
    # No pagination, thus sanity check limit
    return [email async for email in get_email_queryset(user_id=user_id)[:200]]


@router.post(
    "/users/{slug:user_id}/emails/", response={201: EmailAddressSchema}, by_alias=True
)
async def create_email(
    request: AuthHttpRequest, user_id: MeID, payload: EmailAddressIn
):
    """
    Create a new unverified email address. Will return 400 if the email already exists
    and is verified.
    """
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id
    if await EmailAddress.objects.filter(email=payload.email, verified=True).aexists():
        raise HttpError(
            400,
            "Email already exists",
        )
    try:
        email_address = await EmailAddress.objects.acreate(
            email=payload.email, user_id=user_id
        )
    except IntegrityError:
        raise HttpError(
            400,
            "Email already exists",
        )
    await sync_to_async(email_address.send_confirmation)(request, signup=False)
    return 201, email_address


@router.put("/users/{slug:user_id}/emails/", response=EmailAddressSchema, by_alias=True)
async def set_email_as_primary(
    request: AuthHttpRequest, user_id: MeID, payload: EmailAddressIn
):
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id

    queryset = get_email_queryset(user_id)
    email_address = await aget_object_or_404(
        queryset, verified=True, email=payload.email
    )
    await queryset.aupdate(primary=False)
    email_address.primary = True
    await email_address.asave(update_fields=["primary"])
    return email_address


@router.delete("/users/{slug:user_id}/emails/", response={204: None})
async def delete_email(
    request: AuthHttpRequest, user_id: MeID, payload: EmailAddressIn
):
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id

    queryset = get_email_queryset(user_id)
    result, _ = await queryset.filter(email=payload.email, primary=False).adelete()
    if result:
        return 204, None
    raise Http404


@router.post("/users/{slug:user_id}/emails/confirm/", response={204: None})
async def send_confirm_email(
    request: AuthHttpRequest, user_id: MeID, payload: EmailAddressIn
):
    user_id = request.auth.user_id
    email_address = await aget_object_or_404(
        get_email_queryset(user_id, verified=False), email=payload.email
    )
    await sync_to_async(email_address.send_confirmation)(request)
    return 204, None


@router.get(
    "/users/{slug:user_id}/notifications/",
    response=UserNotificationsSchema,
    by_alias=True,
)
async def get_notifications(request: AuthHttpRequest, user_id: MeID):
    user_id = request.auth.user_id
    return await aget_object_or_404(get_user_queryset(user_id))


@router.put(
    "/users/{slug:user_id}/notifications/",
    response=UserNotificationsSchema,
    by_alias=True,
)
async def update_notifications(
    request: AuthHttpRequest, user_id: MeID, payload: UserNotificationsSchema
):
    user_id = request.auth.user_id
    user = await aget_object_or_404(get_user_queryset(user_id))
    for attr, value in payload.dict().items():
        setattr(user, attr, value)
    await user.asave()
    return user
