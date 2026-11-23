import logging
from urllib.parse import urlencode

from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.contrib.auth import aget_user
from django.core.exceptions import RequestDataTooBig
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from ninja import Field, ModelSchema, NinjaAPI, Router, Schema

from apps.alerts.api import router as alerts_router
from apps.api_tokens.api import router as api_tokens_router
from apps.api_tokens.models import APIToken
from apps.api_tokens.schema import APITokenSchema
from apps.difs.api import router as difs_router
from apps.environments.api import router as environments_router
from apps.event_ingest.api import router as event_ingest_router
from apps.event_ingest.embed_api import router as embed_router
from apps.files.api import router as files_router
from apps.importer.api import router as importer_router
from apps.importer.exceptions import ImporterException
from apps.issue_events.api import router as issue_events_router
from apps.logs.api import router as logs_router
from apps.organizations_ext.api import router as organizations_ext_router
from apps.organizations_ext.social_app_api import (
    router as organization_social_apps_router,
)
from apps.performance.api import router as performance_router
from apps.projects.api import router as projects_router
from apps.releases.api import router as releases_router
from apps.sourcecode.api import router as sourcecode_router
from apps.stats.api import router as stats_router
from apps.stripe.api import router as stripe_router
from apps.stripe.models import SupportLicense
from apps.teams.api import router as teams_router
from apps.users.api import router as users_router
from apps.users.models import User
from apps.users.schema import UserSchema
from apps.wizard.api import router as wizard_router
from glitchtip.cold_storage import ColdStorageBusy

from ..schema import CamelSchema
from .authentication import SessionAuth, TokenAuth
from .exceptions import ThrottleException
from .parsers import ORJSONParser

logger = logging.getLogger(__name__)

api = NinjaAPI(
    parser=ORJSONParser(),
    title="GlitchTip API",
    urls_namespace="api",
    auth=[TokenAuth(), SessionAuth()],
    openapi_url="/openapi.json" if settings.ENABLE_OPENAPI else None,
)

api.add_router("0", api_tokens_router)
api.add_router("", event_ingest_router)
api.add_router("0", alerts_router)
api.add_router("0", difs_router)
api.add_router("0", environments_router)
api.add_router("0", files_router)
api.add_router("0", importer_router)
api.add_router("0", issue_events_router)
api.add_router("0", logs_router)
api.add_router("0", organizations_ext_router)
api.add_router("0", organization_social_apps_router)
api.add_router("0", performance_router)
api.add_router("0", projects_router)
api.add_router("0", stats_router)
api.add_router("0/stripe", stripe_router)
api.add_router("0", sourcecode_router)
api.add_router("0", teams_router)

if settings.GLITCHTIP_ENABLE_UPTIME:
    from apps.uptime.api import router as uptime_router

    api.add_router("0", uptime_router)

api.add_router("0", users_router)
api.add_router("0", wizard_router)
api.add_router("0", releases_router)
api.add_router("embed", embed_router)

# Fallback router for unmatched /api/0/... paths - must be added last
# Returns JSON 404 instead of CSRF error page for debugging
fallback_router = Router()


@fallback_router.api_operation(
    ["GET", "POST", "PUT", "PATCH", "DELETE"],
    "/{path:path}",
    auth=None,
    include_in_schema=False,
)
async def api_fallback_404(request: HttpRequest, path: str):
    return api.create_response(request, {"detail": "Not found"}, status=404)


api.add_router("0", fallback_router)


@api.exception_handler(ThrottleException)
def throttled(request: HttpRequest, exc: ThrottleException):
    response = api.create_response(
        request,
        {"message": "Please retry later"},
        status=429,
    )
    if retry_after := exc.retry_after:
        if isinstance(retry_after, int):
            response["Retry-After"] = retry_after
        else:
            response["Retry-After"] = retry_after.strftime("%a, %d %b %Y %H:%M:%S GMT")

    return response


@api.exception_handler(ColdStorageBusy)
def cold_storage_busy(request: HttpRequest, exc: ColdStorageBusy):
    """Report cold-read saturation as transient, not as a missing object.

    These lookups previously returned None when no read slot came free, which
    the views turned into a 404 — telling the client an event or log does not
    exist when in fact the pool was busy. That answer is permanent and
    cacheable by proxies, and indistinguishable from real deletion. 503 with
    Retry-After says what is actually true.
    """
    response = api.create_response(
        request,
        {"message": "Cold storage is busy, please retry"},
        status=503,
    )
    response["Retry-After"] = 1
    return response


@api.exception_handler(RequestDataTooBig)
def request_too_big(request: HttpRequest, exc: RequestDataTooBig):
    return HttpResponse(str(exc), status=413, content_type="text/plain")


@api.exception_handler(ImporterException)
def importer_failed(request: HttpRequest, exc: ImporterException):
    """Report a failed import as a client error.

    Every way an import can fail describes the remote server the caller named --
    a rejected token, a response that isn't the list of results it should be,
    more pages than the importer will follow. None of them are faults in this
    server, so answering 500 both misreports the cause and withholds the one
    detail that would let the caller fix it.
    """
    return api.create_response(request, {"detail": str(exc)}, status=400)


class SocialAppSchema(ModelSchema):
    scopes: list[str]
    brand: str
    """Identity provider brand for display (e.g. picking a logo). Unlike
    `provider`, which is overridden with provider_id for multi-instance
    providers, this always names the underlying identity provider."""

    class Meta:
        model = SocialApp
        fields = ["name", "client_id", "provider"]


class SettingsOut(CamelSchema):
    social_apps: list[SocialAppSchema]
    billing_enabled: bool
    i_paid_for_glitchtip: bool = Field(alias="iPaidForGlitchTip")
    enable_user_registration: bool
    enable_social_apps_user_registration: bool
    enable_organization_creation: bool
    stripe_public_key: str | None
    chatwoot_website_token: str | None
    sentryDSN: str | None
    sentry_traces_sample_rate: float | None
    environment: str | None
    version: str
    server_time_zone: str
    glitchtip_instance_name: str | None
    enabled_features: list[str]


def _serialize_social_app(request: HttpRequest, social_app: SocialApp):
    """
    Annotate a SocialApp with the fields the login page needs. Mutates and
    returns the instance for SocialAppSchema.

    Deliberately makes no outbound request. Resolving an OIDC authorize_url
    means fetching the provider's discovery document, and both callers are
    unauthenticated endpoints that list every app for an instance or an
    organization -- so one anonymous request would fan out into one fetch per
    app, against hosts the app's owner chose. Nothing consumes an authorize
    URL: sign-in posts to allauth's headless endpoint and the server issues
    the redirect.
    """
    social_app.scopes = social_app.get_provider(request).get_scope()
    social_app.brand = (social_app.settings or {}).get("brand") or social_app.provider
    social_app.provider = social_app.provider_id or social_app.provider
    return social_app


@api.get("settings/", response=SettingsOut, by_alias=True, auth=None)
async def get_settings(request: HttpRequest):
    # Organization-linked social apps marked non-public (SaaS per-org SSO)
    # only appear on that organization's own login page, never here.
    social_apps: list[SocialApp] = [
        social_app
        async for social_app in SocialApp.objects.filter(
            Q(organizationsocialapp__isnull=True)
            | Q(organizationsocialapp__is_public=True)
        ).order_by("name")
    ]
    for social_app in social_apps:
        _serialize_social_app(request, social_app)

    billing_enabled = settings.BILLING_ENABLED

    enable_user_registration = settings.ENABLE_USER_REGISTRATION
    enable_social_apps_user_registration = settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION
    if not (enable_user_registration and enable_social_apps_user_registration):
        no_users = not await User.objects.aexists()
        enable_user_registration = enable_user_registration or no_users
        enable_social_apps_user_registration = (
            enable_social_apps_user_registration or no_users
        )

    enabled_features = []
    if settings.GLITCHTIP_ENABLE_LOGS:
        enabled_features.append("logs")
    if settings.GLITCHTIP_ENABLE_UPTIME:
        enabled_features.append("uptime")
    if settings.GLITCHTIP_ENABLE_MCP:
        enabled_features.append("mcp")
    if settings.EMAIL_ENABLED:
        # Signals that email-dependent UI (password reset, resend confirmation)
        # is usable. When absent, the frontend hides those and offers copy-link
        # invites instead.
        enabled_features.append("email")

    # Legacy env wins; fall back to the SupportLicense singleton.
    if settings.BILLING_ENABLED or settings.I_PAID_FOR_GLITCHTIP:
        i_paid_for_glitchtip = True
    else:
        support_license = await SupportLicense.objects.filter(pk=1).afirst()
        i_paid_for_glitchtip = bool(support_license and support_license.license_key)

    return {
        "social_apps": social_apps,
        "billing_enabled": billing_enabled,
        "i_paid_for_glitchtip": i_paid_for_glitchtip,
        "enable_user_registration": enable_user_registration,
        "enable_social_apps_user_registration": enable_social_apps_user_registration,
        "enable_organization_creation": settings.ENABLE_ORGANIZATION_CREATION,
        "stripe_public_key": settings.STRIPE_PUBLIC_KEY,
        "chatwoot_website_token": settings.CHATWOOT_WEBSITE_TOKEN,
        "sentryDSN": settings.SENTRY_FRONTEND_DSN,
        "sentry_traces_sample_rate": settings.SENTRY_TRACES_SAMPLE_RATE,
        "environment": settings.ENVIRONMENT,
        "version": settings.GLITCHTIP_VERSION,
        "server_time_zone": settings.TIME_ZONE,
        "glitchtip_instance_name": settings.GLITCHTIP_INSTANCE_NAME,
        "enabled_features": enabled_features,
    }


class LoginSettingsOut(CamelSchema):
    social_apps: list[SocialAppSchema]


@api.get(
    "settings/{slug:organization_slug}/",
    response=LoginSettingsOut,
    by_alias=True,
    auth=None,
)
async def get_organization_login_settings(request: HttpRequest, organization_slug: str):
    """
    Sign-in options for one organization's own login page, including social
    apps hidden from the instance-wide login page. Responds
    200 with an empty list for unknown slugs so anonymous requests cannot
    enumerate which organizations exist.
    """
    social_apps: list[SocialApp] = [
        social_app
        async for social_app in SocialApp.objects.filter(
            organizationsocialapp__organization__slug=organization_slug,
            organizationsocialapp__organization__is_deleted=False,
        ).order_by("name")
    ]
    for social_app in social_apps:
        _serialize_social_app(request, social_app)
    return {"social_apps": social_apps}


class InstanceLicenseOut(CamelSchema):
    billing_email: str


@api.get("0/instance-license/", response=InstanceLicenseOut, by_alias=True)
async def get_instance_license(request: HttpRequest):
    _, email = await SupportLicense.resolved()
    return {"billing_email": email}


class SupportLinkOut(CamelSchema):
    url: str


@api.get("0/instance-license/support-link/", response=SupportLinkOut, by_alias=True)
async def get_support_link(request: HttpRequest):
    license_key, _ = await SupportLicense.resolved()
    url = "https://glitchtip.com/support"
    if license_key:
        url += f"#{urlencode({'sub': license_key})}"
    return {"url": url}


class APIRootSchema(Schema):
    version: str
    user: UserSchema | None
    auth: APITokenSchema | None


@api.get("0/", auth=None, response=APIRootSchema, by_alias=True)
async def api_root(request: HttpRequest):
    """/api/0/ gives information about the server and current user"""
    user_data = None
    auth_data = None
    user = await aget_user(request)
    if user.is_authenticated:
        user_data = await User.objects.prefetch_related("socialaccount_set").aget(
            id=user.id
        )

    # Fetch api auth header to get api token
    openapi_scheme = "bearer"
    header = "Authorization"
    headers = request.headers
    auth_value = headers.get(header)
    if auth_value:
        parts = auth_value.split(" ")
        if len(parts) >= 2 and parts[0].lower() == openapi_scheme:
            token = " ".join(parts[1:])
            api_token = await APIToken.objects.filter(
                token=token, user__is_active=True
            ).afirst()
            if api_token:
                auth_data = api_token
                user_data = await User.objects.prefetch_related(
                    "socialaccount_set"
                ).aget(id=api_token.user_id)

    return {
        "version": "0",
        "user": user_data,
        "auth": auth_data,
    }
