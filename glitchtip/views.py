from django.conf import settings
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from allauth.socialaccount.models import SocialApp
from users.utils import is_user_registration_open
from users.serializers import SocialAppSerializer, UserSerializer
from api_tokens.serializers import APITokenAuthScopesSerializer

try:
    from djstripe.settings import STRIPE_PUBLIC_KEY
except ImportError:
    pass


class SettingsView(APIView):
    """ Global configuration to pass to client """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        billing_enabled = settings.BILLING_ENABLED
        enable_user_registration = is_user_registration_open()
        stripe_public_key = None
        if billing_enabled:
            stripe_public_key = STRIPE_PUBLIC_KEY
        social_apps = SocialAppSerializer(SocialApp.objects.all(), many=True).data
        return Response(
            {
                "socialApps": social_apps,
                "billingEnabled": billing_enabled,
                "enableUserRegistration": enable_user_registration,
                "stripePublicKey": stripe_public_key,
                "matomoURL": settings.MATOMO_URL,
                "matomoSiteId": settings.MATOMO_SITE_ID,
                "rocketChatDomain": settings.ROCKET_CHAT_DOMAIN,
                "sentryDSN": settings.SENTRY_FRONTEND_DSN,
                "sentryTracesSampleRate": settings.SENTRY_TRACES_SAMPLE_RATE,
                "environment": settings.ENVIRONMENT,
                "version": settings.GLITCHTIP_VERSION,
            }
        )


class APIRootView(APIView):
    """ /api/0/ gives information about the server and current user """

    def get(self, request, format=None):
        user_data = None
        auth_data = None
        if request.user.is_authenticated:
            user_data = UserSerializer(instance=request.user).data
        if request.auth:
            auth_data = APITokenAuthScopesSerializer(instance=request.auth).data
        return Response({"version": "0", "user": user_data, "auth": auth_data,})


def health(request):
    return HttpResponse("ok", content_type="text/plain")
