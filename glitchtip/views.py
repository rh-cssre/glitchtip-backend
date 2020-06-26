from django.conf import settings
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from users.utils import is_user_registration_open

try:
    from djstripe.settings import STRIPE_PUBLIC_KEY
except ImportError:
    pass


class SettingsView(APIView):
    """ Global configuration to pass to client """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        social_auth = settings.ENABLE_SOCIAL_AUTH
        billing_enabled = settings.BILLING_ENABLED
        enable_user_registration = is_user_registration_open()
        stripe_public_key = None
        if billing_enabled:
            stripe_public_key = STRIPE_PUBLIC_KEY
        return Response(
            {
                "socialAuth": social_auth,
                "billingEnabled": billing_enabled,
                "enableUserRegistration": enable_user_registration,
                "stripePublicKey": stripe_public_key,
                "matomoURL": settings.MATOMO_URL,
                "matomoSiteId": settings.MATOMO_SITE_ID,
                "sentryDSN": settings.SENTRY_FRONTEND_DSN,
            }
        )


def health(request):
    return HttpResponse("ok", content_type="text/plain")
