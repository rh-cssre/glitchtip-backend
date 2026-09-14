"""
Organization-managed single sign-on.

Lets organization managers configure their own OpenID Connect provider
(Okta, Entra ID, Auth0, Keycloak, Google Workspace, ...) without instance
admin access. The created SocialApp is linked to the organization through
OrganizationSocialApp, which both auto-joins users who authenticate with it
(see apps.users.signals) and, when non-public, keeps it off the instance-wide
login page in favor of the organization's own login page.
"""

import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit

from allauth.socialaccount.models import SocialApp
from asgiref.sync import sync_to_async
from django.db import transaction
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import HttpError

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .constants import OrganizationUserRole
from .models import Organization, OrganizationSocialApp
from .queryset_utils import get_organizations_queryset
from .schema import (
    OrganizationSocialAppIn,
    OrganizationSocialAppSchema,
    OrganizationSocialAppUpdateIn,
)
from .utils import organization_can_customize_sso

router = Router()

MAX_SOCIAL_APPS_PER_ORGANIZATION = 10

# Preconfigured brands: identity providers that speak OIDC and have a fixed,
# well-known issuer, so org managers only supply a client id and secret.
PRECONFIGURED_SERVER_URLS = {
    "google": "https://accounts.google.com",
}

"""
GET /api/0/organizations/{organization_slug}/social-apps/
POST /api/0/organizations/{organization_slug}/social-apps/
PUT /api/0/organizations/{organization_slug}/social-apps/{social_app_id}/
DELETE /api/0/organizations/{organization_slug}/social-apps/{social_app_id}/
"""


def validate_server_url(url: str) -> str:
    """
    Sanity-check an org-supplied OIDC issuer / discovery URL.

    Fast feedback only, NOT the SSRF control: a public hostname may still
    resolve to a private address, and the discovery document's endpoints are
    attacker-chosen. The real enforcement is connection-time blocking of
    non-public destinations (SOCIAL_AUTH_BLOCK_PRIVATE_IPS, on by default
    whenever non-admin org managers can exist).
    """
    url = url.strip()
    while url.endswith("/"):
        url = url[:-1]
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise HttpError(400, "Server URL must be an https:// URL")
    if "@" in parts.netloc:
        raise HttpError(400, "Server URL must not contain credentials")
    hostname = parts.hostname
    try:
        ip = ip_address(hostname)
    except ValueError:
        if "." not in hostname or hostname.endswith((".localhost", ".internal")):
            raise HttpError(400, "Server URL must be a public hostname")
    else:
        if not ip.is_global:
            raise HttpError(400, "Server URL must be a public address")
    return url


async def get_organization_for_manager(
    request: AuthHttpRequest, organization_slug: str
) -> Organization:
    organization = await aget_object_or_404(
        get_organizations_queryset(
            request.auth.user_id,
            role_required=True,
            organization_slug=organization_slug,
        )
    )
    if organization.actor_role < OrganizationUserRole.MANAGER:
        raise HttpError(403, "forbidden")
    return organization


def get_social_apps_queryset(organization: Organization):
    return (
        SocialApp.objects.filter(organizationsocialapp__organization=organization)
        .select_related("organizationsocialapp__organization")
        .order_by("name")
    )


async def generate_provider_id(organization: Organization) -> str:
    """
    Unique SocialApp.provider_id, which becomes part of the OIDC callback URL
    registered at the IdP — readable, instance-unique, and never regenerated
    for the app's lifetime.
    """
    for _ in range(10):
        provider_id = f"{organization.slug[:150]}-{secrets.token_hex(3)}"
        if not await SocialApp.objects.filter(provider_id=provider_id).aexists():
            return provider_id
    raise HttpError(500, "Unable to generate a unique provider id")


@sync_to_async
def create_linked_social_app(
    organization: Organization, provider_id: str, payload, app_settings: dict
) -> SocialApp:
    """
    Create a SocialApp and its organization link as one unit.

    Both rows must land together: a SocialApp with no OrganizationSocialApp is
    indistinguishable from an instance-wide app, so a half-finished create would
    leave the organization's provider listed on the instance-wide login page.

    Sync because the transaction has to span allauth's SocialApp, which cannot
    carry an async manager. async_atomic() covers async_connections, while
    Django's native acreate() runs on the sync connection, so it would not
    actually enclose these writes. Nothing awaits inside, so the transaction
    opens and closes within this one thread-sensitive call.

    is_public is deliberately not settable here: listing a sign-in option on the
    instance-wide login page is a server-level decision, so it stays an
    instance-admin action in Django admin.
    """
    with transaction.atomic():
        social_app = SocialApp.objects.create(
            provider="openid_connect",
            provider_id=provider_id,
            name=payload.name,
            client_id=payload.client_id,
            secret=payload.client_secret,
            settings=app_settings,
        )
        OrganizationSocialApp.objects.create(
            organization=organization, social_app=social_app
        )
    return social_app


@router.get(
    "organizations/{slug:organization_slug}/social-apps/",
    response=list[OrganizationSocialAppSchema],
    by_alias=True,
)
@has_permission(["org:read", "org:write", "org:admin"])
async def list_organization_social_apps(
    request: AuthHttpRequest, organization_slug: str
):
    organization = await get_organization_for_manager(request, organization_slug)
    return [app async for app in get_social_apps_queryset(organization)]


@router.post(
    "organizations/{slug:organization_slug}/social-apps/",
    response={201: OrganizationSocialAppSchema},
    by_alias=True,
)
@has_permission(["org:write", "org:admin"])
async def create_organization_social_app(
    request: AuthHttpRequest, organization_slug: str, payload: OrganizationSocialAppIn
):
    organization = await get_organization_for_manager(request, organization_slug)
    if not await organization_can_customize_sso(organization):
        raise HttpError(403, "Custom SSO is not included in your plan")
    if (
        await OrganizationSocialApp.objects.filter(organization=organization).acount()
        >= MAX_SOCIAL_APPS_PER_ORGANIZATION
    ):
        raise HttpError(400, "Maximum number of SSO providers reached")
    app_settings = {}
    if preconfigured_url := PRECONFIGURED_SERVER_URLS.get(payload.provider):
        app_settings["server_url"] = preconfigured_url
        app_settings["brand"] = payload.provider
    elif payload.server_url:
        app_settings["server_url"] = validate_server_url(payload.server_url)
    else:
        raise HttpError(400, "Server URL is required")
    social_app = await create_linked_social_app(
        organization,
        provider_id=await generate_provider_id(organization),
        payload=payload,
        app_settings=app_settings,
    )
    return Status(
        201, await get_social_apps_queryset(organization).aget(id=social_app.id)
    )


@router.put(
    "organizations/{slug:organization_slug}/social-apps/{int:social_app_id}/",
    response=OrganizationSocialAppSchema,
    by_alias=True,
)
@has_permission(["org:write", "org:admin"])
async def update_organization_social_app(
    request: AuthHttpRequest,
    organization_slug: str,
    social_app_id: int,
    payload: OrganizationSocialAppUpdateIn,
):
    organization = await get_organization_for_manager(request, organization_slug)
    social_app = await aget_object_or_404(
        get_social_apps_queryset(organization), id=social_app_id
    )
    if social_app.provider != "openid_connect":
        raise HttpError(400, "Only OpenID Connect apps can be edited with this API")
    social_app.name = payload.name
    social_app.client_id = payload.client_id
    if (social_app.settings or {}).get("brand") in PRECONFIGURED_SERVER_URLS:
        pass  # The issuer of a preconfigured brand is fixed
    elif payload.server_url:
        social_app.settings = {
            **(social_app.settings or {}),
            "server_url": validate_server_url(payload.server_url),
        }
    else:
        raise HttpError(400, "Server URL is required")
    if payload.client_secret:
        social_app.secret = payload.client_secret
    await social_app.asave(update_fields=["name", "client_id", "secret", "settings"])
    return social_app


@router.delete(
    "organizations/{slug:organization_slug}/social-apps/{int:social_app_id}/",
    response={204: None},
)
@has_permission(["org:write", "org:admin"])
async def delete_organization_social_app(
    request: AuthHttpRequest, organization_slug: str, social_app_id: int
):
    organization = await get_organization_for_manager(request, organization_slug)
    social_app = await aget_object_or_404(
        get_social_apps_queryset(organization), id=social_app_id
    )
    # Cascades to OrganizationSocialApp. Existing user accounts and their
    # SocialAccount links remain; only this sign-in route goes away.
    await social_app.adelete()
    return Status(204, None)
