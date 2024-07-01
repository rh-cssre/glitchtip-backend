from typing import Optional

from django.db.models import Count, Exists, OuterRef, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError
from organizations.signals import user_added

from apps.projects.models import Project
from apps.teams.models import Team
from apps.users.models import User
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission

from .models import Organization, OrganizationUser, OrganizationUserRole
from .schema import OrganizationDetailSchema, OrganizationInSchema, OrganizationSchema
from .utils import is_organization_creation_open

router = Router()

"""
GET /api/0/organizations/
POST /api/0/organizations/ (Not in sentry)
GET /api/0/organizations/{organization_slug}/
PUT /api/0/organizations/{organization_slug}/
DELETE /api/0/organizations/{organization_slug}/ (Not in sentry)
"""


def get_organizations_queryset(
    user_id, role_required: OrganizationUserRole = None, add_details=False
):
    qs = Organization.objects.filter(users=user_id)
    if role_required:
        qs = qs.filter(
            organization_users__user=user_id,
            organization_users__role__gte=role_required,
        )
    if add_details:
        qs = qs.prefetch_related(
            Prefetch(
                "projects",
                queryset=Project.annotate_is_member(Project.objects, user_id),
            ),
            "projects__teams",
            Prefetch(
                "teams",
                queryset=Team.objects.annotate(
                    is_member=Exists(
                        OrganizationUser.objects.filter(
                            teams=OuterRef("pk"), user_id=user_id
                        )
                    ),
                    member_count=Count("members"),
                ),
            ),
            "teams__members",
        )
    return qs


@router.get("organizations/", response=list[OrganizationSchema], by_alias=True)
@paginate
@has_permission(["org:read", "org:write", "org:admin"])
async def list_organizations(
    request: AuthHttpRequest,
    response: HttpResponse,
    owner: Optional[bool] = None,
    query: Optional[str] = None,
    sortBy: Optional[str] = None,
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
            role_required=OrganizationUserRole.MANAGER,
            add_details=True,
        ),
        slug=organization_slug,
    )
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
    result, _ = (
        await get_organizations_queryset(
            request.auth.user_id, role_required=OrganizationUserRole.MANAGER
        )
        .filter(
            slug=organization_slug,
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None
