import logging

from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.openid_connect.views import (
    OpenIDConnectOAuth2Adapter,
)
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import aget_user
from django.http import HttpRequest
from ninja import Field, ModelSchema, NinjaAPI, Schema

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
from apps.issue_events.api import router as issue_events_router
from apps.observability.api import router as observability_router
from apps.organizations_ext.api import router as organizations_ext_router
from apps.performance.api import router as performance_router
from apps.projects.api import router as projects_router
from apps.releases.api import router as releases_router
from apps.sourcecode.api import router as sourcecode_router
from apps.stats.api import router as stats_router
from apps.stripe.api import router as stripe_router
from apps.teams.api import router as teams_router
from apps.uptime.api import router as uptime_router
from apps.users.api import router as users_router
from apps.users.models import User
from apps.users.schema import UserSchema
from apps.wizard.api import router as wizard_router
from glitchtip.constants import SOCIAL_ADAPTER_MAP

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
)

api.add_router("0", api_tokens_router)
api.add_router("", event_ingest_router)
api.add_router("0", alerts_router)
api.add_router("0", difs_router)
api.add_router("0", environments_router)
api.add_router("0", files_router)
api.add_router("0", importer_router)
api.add_router("0", issue_events_router)
api.add_router("0", observability_router)
api.add_router("0", organizations_ext_router)
api.add_router("0", performance_router)
api.add_router("0", projects_router)
api.add_router("0", stats_router)
api.add_router("0/stripe", stripe_router)
api.add_router("0", sourcecode_router)
api.add_router("0", teams_router)
api.add_router("0", uptime_router)
api.add_router("0", users_router)
api.add_router("0", wizard_router)
api.add_router("0", releases_router)
api.add_router("embed", embed_router)


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


class SocialAppSchema(ModelSchema):
    scopes: list[str]
    authorize_url: str | None

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
    plausible_url: str | None
    plausible_domain: str | None
    chatwoot_website_token: str | None
    sentryDSN: str | None
    sentry_traces_sample_rate: float | None
    environment: str | None
    version: str
    server_time_zone: str
    glitchtip_instance_name: str | None


@api.get("settings/", response=SettingsOut, by_alias=True, auth=None)
async def get_settings(request: HttpRequest):
    social_apps: list[SocialApp] = []
    async for social_app in SocialApp.objects.order_by("name"):
        provider = social_app.get_provider(request)
        social_app.scopes = provider.get_scope()

        adapter_cls = SOCIAL_ADAPTER_MAP.get(social_app.provider)
        if adapter_cls == OpenIDConnectOAuth2Adapter:
            adapter = adapter_cls(request, social_app.provider_id)
        elif adapter_cls:
            adapter = adapter_cls(request)
        else:
            adapter = None
        if adapter:
            social_app.authorize_url = await sync_to_async(
                lambda: adapter.authorize_url
            )()

        social_app.provider = social_app.provider_id or social_app.provider
        social_apps.append(social_app)

    billing_enabled = settings.BILLING_ENABLED

    enable_user_registration = settings.ENABLE_USER_REGISTRATION
    enable_social_apps_user_registration = settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION
    if not (enable_user_registration and enable_social_apps_user_registration):
        no_users = not await User.objects.aexists()
        enable_user_registration = enable_user_registration or no_users
        enable_social_apps_user_registration = (
            enable_social_apps_user_registration or no_users
        )

    return {
        "social_apps": social_apps,
        "billing_enabled": billing_enabled,
        "i_paid_for_glitchtip": settings.I_PAID_FOR_GLITCHTIP,
        "enable_user_registration": enable_user_registration,
        "enable_social_apps_user_registration": enable_social_apps_user_registration,
        "enable_organization_creation": settings.ENABLE_ORGANIZATION_CREATION,
        "stripe_public_key": settings.STRIPE_PUBLIC_KEY,
        "plausible_url": settings.PLAUSIBLE_URL,
        "plausible_domain": settings.PLAUSIBLE_DOMAIN,
        "chatwoot_website_token": settings.CHATWOOT_WEBSITE_TOKEN,
        "sentryDSN": settings.SENTRY_FRONTEND_DSN,
        "sentry_traces_sample_rate": settings.SENTRY_TRACES_SAMPLE_RATE,
        "environment": settings.ENVIRONMENT,
        "version": settings.GLITCHTIP_VERSION,
        "server_time_zone": settings.TIME_ZONE,
        "glitchtip_instance_name": settings.GLITCHTIP_INSTANCE_NAME,
    }


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
