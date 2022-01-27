from allauth.socialaccount.models import SocialApp
from django.conf import settings
from django.http import HttpResponse
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api_tokens.serializers import APITokenAuthScopesSerializer
from users.serializers import SocialAppSerializer, UserSerializer
from users.utils import is_user_registration_open

try:
    from djstripe.settings import djstripe_settings
except ImportError:
    pass


class SettingsView(APIView):
    """ Global configuration to pass to client """

    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        billing_enabled = settings.BILLING_ENABLED
        enable_user_registration = is_user_registration_open()
        stripe_public_key = None
        if billing_enabled:
            stripe_public_key = djstripe_settings.STRIPE_PUBLIC_KEY
        social_apps = SocialAppSerializer(
            SocialApp.objects.all().order_by("name"),
            many=True,
            context={"request": request},
        ).data
        return Response(
            {
                "socialApps": social_apps,
                "billingEnabled": billing_enabled,
                "iPaidForGlitchTip": settings.I_PAID_FOR_GLITCHTIP,
                "enableUserRegistration": enable_user_registration,
                "stripePublicKey": stripe_public_key,
                "plausibleURL": settings.PLAUSIBLE_URL,
                "plausibleDomain": settings.PLAUSIBLE_DOMAIN,
                "chatwootWebsiteToken": settings.CHATWOOT_WEBSITE_TOKEN,
                "sentryDSN": settings.SENTRY_FRONTEND_DSN,
                "sentryTracesSampleRate": settings.SENTRY_TRACES_SAMPLE_RATE,
                "environment": settings.ENVIRONMENT,
                "version": settings.GLITCHTIP_VERSION,
            }
        )


class APIRootView(APIView):
    """ /api/0/ gives information about the server and current user """

    def get(self, request, *args, **kwargs):
        user_data = None
        auth_data = None
        if request.user.is_authenticated:
            user_data = UserSerializer(instance=request.user).data
        if request.auth:
            auth_data = APITokenAuthScopesSerializer(instance=request.auth).data
        return Response({"version": "0", "user": user_data, "auth": auth_data,})


def health(request):
    return HttpResponse("ok", content_type="text/plain")
