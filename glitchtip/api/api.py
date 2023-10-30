from typing import Optional

from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.openid_connect.views import OpenIDConnectAdapter
from django.conf import settings
from django.http import HttpRequest
from ninja import ModelSchema, NinjaAPI, Schema

from glitchtip.constants import SOCIAL_ADAPTER_MAP
from users.utils import ais_user_registration_open

from .exceptions import ThrottleException
from .parsers import EnvelopeParser
from .schema import CamelSchema

try:
    from djstripe.settings import djstripe_settings
except ImportError:
    pass


api = NinjaAPI(parser=EnvelopeParser(), title="GlitchTip API", urls_namespace="api")
api.add_router("", "glitchtip.event_ingest.api.router")


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
    authorize_url: Optional[str]

    class Config:
        model = SocialApp
        model_fields = ["name", "client_id", "provider"]


class SettingsOut(CamelSchema):
    social_apps: list[SocialAppSchema]
    billing_enabled: bool
    i_paid_for_glitchtip: bool
    enable_user_registration: bool
    enable_organization_creation: bool
    stripe_public_key: Optional[str]
    plausible_url: Optional[str]
    plausible_domain: Optional[str]
    chatwoot_website_token: Optional[str]
    sentryDSN: Optional[str]
    sentry_traces_sample_rate: Optional[float]
    environment: Optional[str]
    version: str
    server_time_zone: str


@api.get("settings/", response=SettingsOut, by_alias=True)
async def get_settings(request: HttpRequest):
    social_apps: list[SocialApp] = []
    async for social_app in SocialApp.objects.order_by("name"):
        provider = social_app.get_provider(request)
        social_app.scopes = provider.get_scope(request)

        adapter_cls = SOCIAL_ADAPTER_MAP.get(social_app.provider)
        if adapter_cls == OpenIDConnectAdapter:
            adapter = adapter_cls(request, social_app.provider_id)
        elif adapter_cls:
            adapter = adapter_cls(request)
        else:
            adapter = None
        if adapter:
            social_app.authorize_url = adapter.authorize_url

        social_app.provider = social_app.provider_id or social_app.provider
        social_apps.append(social_app)

    billing_enabled = settings.BILLING_ENABLED

    return {
        "social_apps": social_apps,
        "billing_enabled": billing_enabled,
        "i_paid_for_glitchtip": settings.I_PAID_FOR_GLITCHTIP,
        "enable_user_registration": await ais_user_registration_open(),
        "enable_organization_creation": settings.ENABLE_ORGANIZATION_CREATION,
        "stripe_public_key": djstripe_settings.STRIPE_PUBLIC_KEY
        if billing_enabled
        else None,
        "plausible_url": settings.PLAUSIBLE_URL,
        "plausible_domain": settings.PLAUSIBLE_DOMAIN,
        "chatwoot_website_token": settings.CHATWOOT_WEBSITE_TOKEN,
        "sentryDSN": settings.SENTRY_FRONTEND_DSN,
        "sentry_traces_sample_rate": settings.SENTRY_TRACES_SAMPLE_RATE,
        "environment": settings.ENVIRONMENT,
        "version": settings.GLITCHTIP_VERSION,
        "server_time_zone": settings.TIME_ZONE,
    }
