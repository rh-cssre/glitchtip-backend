from django.conf import settings
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny


class SettingsView(APIView):
    """ Global configuration to pass to client """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        social_auth = settings.ENABLE_SOCIAL_AUTH
        billing_enabled = settings.BILLING_ENABLED
        return Response(
            {
                "socialAuth": social_auth,
                "billingEnabled": billing_enabled,
                "matomoURL": settings.MATOMO_URL,
                "matomoSiteId": settings.MATOMO_SITE_ID,
            }
        )


def health(request):
    return HttpResponse("ok", content_type="text/plain")
