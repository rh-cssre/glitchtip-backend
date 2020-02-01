from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny


class SettingsView(APIView):
    """ Global configuration to pass to client """

    permission_classes = [AllowAny]

    def get(self, request, format=None):
        social_auth = settings.ENABLE_SOCIAL_AUTH
        return Response({"socialAuth": social_auth})
