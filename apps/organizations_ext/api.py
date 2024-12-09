from asgiref.sync import sync_to_async
from django.contrib.auth import aget_user
from django.http import HttpRequest, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError, ValidationError
from ninja.pagination import paginate
from organizations.backends import invitation_backend
from organizations.signals import owner_changed, user_added

from apps.teams.models import Team
from apps.teams.schema import OrganizationDetailSchema
from apps.users.models import User
from apps.users.utils import ais_user_registration_open
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .constants import OrganizationUserRole
from .invitation_backend import InvitationTokenGenerator
from .models import Organization, OrganizationOwner, OrganizationUser
from .queryset_utils import get_organization_users_queryset, get_organizations_queryset
from .schema import (
    AcceptInviteIn,
    AcceptInviteSchema,
    OrganizationInSchema,
    OrganizationSchema,
    OrganizationUserDetailSchema,
    OrganizationUserIn,
    OrganizationUserSchema,
    OrganizationUserUpdateSchema,
)
from .utils import is_organization_creation_open

router = Router()

"""
GET /api/0/organizations/
POST /api/0/organizations/ (Not in sentry)
GET /api/0/organizations/{organization_slug}/
PUT /api/0/organizations/{organization_slug}/
DELETE /api/0/organizations/{organization_slug}/ (Not in sentry)
GET /api/0/organizations/{organization_slug}/members/
GET /api/0/organizations/{organization_slug}/members/{member_id}/
POST /api/0/organizations/{organization_slug}/members/{member_id}/
DELETE /api/0/organizations/{organization_slug}/members/{member_id}/
GET /api/0/teams/{organization_slug}/{team_slug}/members/ (Not documented in sentry)
"""


@router.get("organizations/", response=list[OrganizationSchema], by_alias=True)
@paginate
@has_permission(["org:read", "org:write", "org:admin"])
async def list_organizations(
    request: AuthHttpRequest,
    response: HttpResponse,
    owner: bool | None = None,
    query: str | None = None,
    sortBy: str | None = None,
):
    """Return list of all organizations the user has access to."""
    return get_organizations_queryset(request.auth.user_id).order_by("name")


@router.get(
    "organizations/{slug:organization_slug}/",
    response=OrganizationDetailSchema,
    by_alias=True,
)
@has_permission(["org:read", "org:write", "org:admin"])
async def get_organization(request: AuthHttpRequest, organization_slug: str):
    """Return Organization with project and team details."""
    return await aget_object_or_404(
        get_organizations_queryset(request.auth.user_id, add_details=True),
        slug=organization_slug,
    )


@router.post("organizations/", response={201: OrganizationDetailSchema}, by_alias=True)
@has_permission(["org:write", "org:admin"])
async def create_organization(request: AuthHttpRequest, payload: OrganizationInSchema):
    """
    Create new organization
    The first organization on a server is always allowed to be created.
    Afterwards, ENABLE_OPEN_USER_REGISTRATION is checked.
    Superusers are always allowed to create organizations.
    """
    user = await aget_object_or_404(User, id=request.auth.user_id)
    if not await is_organization_creation_open() and not user.is_superuser:
        raise HttpError(403, "Organization creation is not open")
    organization = await Organization.objects.acreate(**payload.dict())

    org_user = await organization._org_user_model.objects.acreate(
        user=user, organization=organization, role=OrganizationUserRole.OWNER
    )
    await organization._org_owner_model.objects.acreate(
        organization=organization, organization_user=org_user
    )
    user_added.send(sender=organization, user=user)

    return 201, await get_organizations_queryset(user.id, add_details=True).aget(
        id=organization.id
    )


@router.put(
    "organizations/{slug:organization_slug}/",
    response=OrganizationDetailSchema,
    by_alias=True,
)
@has_permission(["org:write", "org:admin"])
async def update_organization(
    request: AuthHttpRequest, organization_slug: str, payload: OrganizationInSchema
):
    """Update an organization."""
    organization = await aget_object_or_404(
        get_organizations_queryset(
            request.auth.user_id,
            role_required=True,
            add_details=True,
            organization_slug=organization_slug,
        ),
        slug=organization_slug,
    )
    if organization.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "forbidden")
    for attr, value in payload.dict().items():
        setattr(organization, attr, value)
    await organization.asave()
    return organization


@router.delete(
    "organizations/{slug:organization_slug}/",
    response={204: None},
)
@has_permission(["org:admin"])
async def delete_organization(request: AuthHttpRequest, organization_slug: str):
    organization = await aget_object_or_404(
        get_organizations_queryset(
            request.auth.user_id,
            role_required=True,
            organization_slug=organization_slug,
        )
    )
    if organization.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "forbidden")
    await organization.adelete()
    return 204, None


@router.get(
    "organizations/{slug:organization_slug}/members/",
    response=list[OrganizationUserSchema],
    by_alias=True,
)
@paginate
@has_permission(["member:read", "member:write", "member:admin"])
async def list_organization_members(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_organization_users_queryset(request.auth.user_id, organization_slug)


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/members/",
    response=list[OrganizationUserSchema],
    by_alias=True,
)
@paginate
@has_permission(["member:read", "member:write", "member:admin"])
async def list_team_organization_members(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    team_slug: str,
):
    return get_organization_users_queryset(
        request.auth.user_id, organization_slug, team_slug=team_slug
    )


@router.get(
    "organizations/{slug:organization_slug}/members/{int:member_id}/",
    response=OrganizationUserDetailSchema,
    by_alias=True,
)
@has_permission(["member:read", "member:write", "member:admin"])
async def get_organization_member(
    request: AuthHttpRequest, organization_slug: str, member_id: int
):
    user_id = request.auth.user_id
    return await aget_object_or_404(
        get_organization_users_queryset(user_id, organization_slug, add_details=True),
        pk=member_id,
    )


@router.post(
    "organizations/{slug:organization_slug}/members/",
    response={201: OrganizationUserSchema},
    by_alias=True,
)
@has_permission(["member:write", "member:admin"])
async def create_organization_member(
    request: AuthHttpRequest, organization_slug: str, payload: OrganizationUserIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        get_organizations_queryset(
            user_id, role_required=True, organization_slug=organization_slug
        )
        .filter(organization_users__user=user_id)
        .prefetch_related("organization_users"),
    )
    if organization.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "forbidden")
    email = payload.email
    if (
        not await ais_user_registration_open()
        and not await User.objects.filter(email=email).aexists()
    ):
        raise HttpError(403, "Only existing users may be invited")
    if await organization.organization_users.filter(user__email=email).aexists():
        raise HttpError(
            409,
            f"The user {email} is already a member",
        )
    member, created = await OrganizationUser.objects.aget_or_create(
        email=email,
        organization=organization,
        defaults={"role": OrganizationUserRole.from_string(payload.org_role)},
    )
    if not created and not payload.reinvite:
        raise HttpError(
            409,
            f"The user {email} is already invited",
        )
    teams = [
        team
        async for team in Team.objects.filter(
            slug__in=[role.team_slug for role in payload.team_roles],
            organization=organization,
        ).values_list("pk", flat=True)
    ]
    if teams:
        await member.teams.aadd(*teams)

    await sync_to_async(invitation_backend().send_invitation)(member)
    return 201, member


@router.delete(
    "organizations/{slug:organization_slug}/members/{int:member_id}/",
    response={204: None},
)
@has_permission(["member:admin"])
async def delete_organization_member(
    request: AuthHttpRequest, organization_slug: str, member_id: int
):
    """Remove member (user) from organization"""
    user_id = request.auth.user_id
    if await OrganizationOwner.objects.filter(
        organization_user__user_id=user_id,
        organization__slug=organization_slug,
        organization_user__id=member_id,
    ).aexists():
        raise HttpError(400, "User is organization owner. Transfer ownership first.")
    org_user = await aget_object_or_404(
        get_organization_users_queryset(user_id, organization_slug, role_required=True),
        id=member_id,
    )
    if org_user.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "Forbidden")
    await org_user.adelete()

    return 204, None


@router.put(
    "organizations/{slug:organization_slug}/members/{int:member_id}/",
    response=OrganizationUserDetailSchema,
    by_alias=True,
)
@has_permission(["member:write", "member:admin"])
async def update_organization_member(
    request: AuthHttpRequest,
    organization_slug: str,
    member_id: int,
    payload: OrganizationUserUpdateSchema,
):
    """Update member role within organization"""
    member = await aget_object_or_404(
        get_organization_users_queryset(
            request.auth.user_id,
            organization_slug,
            role_required=True,
            add_details=True,
        ).select_related("organization"),
        id=member_id,
    )
    if member.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "Forbidden")
    member.role = OrganizationUserRole.from_string(payload.org_role)
    # Disallow an ownerless organization
    if (
        member.role < OrganizationUserRole.OWNER
        and not await OrganizationUser.objects.exclude(id=member_id)
        .filter(
            organization__slug=organization_slug, role__gte=OrganizationUserRole.OWNER
        )
        .aexists()
    ):
        raise ValidationError("Organization must have at least one owner")
    await member.asave()
    return member


@router.post(
    "organizations/{slug:organization_slug}/members/{int:member_id}/set_owner/",
    response=OrganizationUserDetailSchema,
    by_alias=True,
)
@has_permission(["member:admin"])
async def set_organization_owner(
    request: AuthHttpRequest, organization_slug: str, member_id: int
):
    """
    Set this team member as the one and only one Organization owner
    Only an existing Owner or user with the "org:admin" scope is able to perform this.
    GlitchTip specific API, no sentry api compatibility
    """
    user_id = request.auth.user_id
    new_owner = await aget_object_or_404(
        get_organization_users_queryset(
            user_id, organization_slug, add_details=True
        ).select_related("organization__owner__organization_user"),
        id=member_id,
    )
    organization = new_owner.organization
    old_owner = organization.owner.organization_user
    if not (
        old_owner.pk is user_id
        or await organization.organization_users.filter(
            user=user_id, role=OrganizationUserRole.OWNER
        ).aexists()
    ):
        raise HttpError(403, "Only owner may set organization owner.")

    organization.owner.organization_user = new_owner
    await organization.owner.asave()
    owner_changed.send(sender=organization, old=old_owner, new=new_owner)
    return new_owner


async def validate_token(org_user_id: int, token: str) -> OrganizationUser:
    """Validate invite token and return org user"""
    org_user = await aget_object_or_404(
        OrganizationUser.objects.all()
        .select_related("organization", "user")
        .prefetch_related("user__socialaccount_set"),
        pk=org_user_id,
    )
    if not InvitationTokenGenerator().check_token(org_user, token):
        raise HttpError(403, "Invalid invite token")
    return org_user


@router.get(
    "accept/{int:org_user_id}/{str:token}/",
    response=AcceptInviteSchema,
    by_alias=True,
    auth=None,
)
async def get_accept_invite(request: HttpRequest, org_user_id: int, token: str):
    """Return relevant organization data around an invite"""
    org_user = await validate_token(org_user_id, token)
    return {"accept_invite": False, "org_user": org_user}


@router.post(
    "accept/{int:org_user_id}/{str:token}/",
    response=AcceptInviteSchema,
    by_alias=True,
)
async def accept_invite(
    request: AuthHttpRequest, org_user_id: int, token: str, payload: AcceptInviteIn
):
    """Accepts invite to organization"""
    org_user = await validate_token(org_user_id, token)
    if payload.accept_invite:
        org_user.user = await aget_user(request)
        org_user.email = None
        await org_user.asave()
    org_user = (
        await OrganizationUser.objects.filter(pk=org_user.pk)
        .select_related("organization", "user")
        .prefetch_related("user__socialaccount_set")
        .aget()
    )
    return {"accept_invite": payload.accept_invite, "org_user": org_user}
