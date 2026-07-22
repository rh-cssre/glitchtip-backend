"""
Organization-managed single sign-on.

Lets organization managers configure their own OpenID Connect provider
(Okta, Entra ID, Auth0, Keycloak, Google Workspace, ...) without instance
admin access. The created SocialApp is linked to the organization through
OrganizationSocialApp, which both auto-joins users who authenticate with it
(see apps.users.signals) and, when non-public, keeps it off the instance-wide
login page in favor of /login/<org slug>.
"""

import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit

from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.shortcuts import aget_object_or_404
from ninja import Router
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


def is_public_login_page_allowed() -> bool:
    """
    Whether org-managed apps may appear on the instance-wide login page.

    Only on effectively single-tenant self-host installs. Anywhere untrusted
    org managers can exist (billing, or open organization creation) a manager
    could otherwise plant a convincingly-named sign-in button on the global
    login page that authenticates victims at the manager's own IdP and
    auto-joins them to the manager's organization. Instance admins can still
    set is_public in Django admin.
    """
    return not settings.BILLING_ENABLED and not settings.ENABLE_ORGANIZATION_CREATION


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
    social_app = await SocialApp.objects.acreate(
        provider="openid_connect",
        provider_id=await generate_provider_id(organization),
        name=payload.name,
        client_id=payload.client_id,
        secret=payload.client_secret,
        settings=app_settings,
    )
    await OrganizationSocialApp.objects.acreate(
        organization=organization,
        social_app=social_app,
        is_public=payload.is_public and is_public_login_page_allowed(),
    )
    return 201, await get_social_apps_queryset(organization).aget(id=social_app.id)


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

    # Where the toggle isn't offered, leave is_public alone so an instance
    # admin's deliberate Django-admin override survives org-side edits.
    if is_public_login_page_allowed():
        organization_social_app = social_app.organizationsocialapp
        if organization_social_app.is_public != payload.is_public:
            organization_social_app.is_public = payload.is_public
            await organization_social_app.asave(update_fields=["is_public"])
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
    return 204, None
