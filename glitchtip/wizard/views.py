import string
from collections import namedtuple

from django.core.cache import cache
from django.utils.crypto import get_random_string
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from api_tokens.models import APIToken
from projects.models import Project

from .serializers import SetupWizardResultSerializer, SetupWizardSerializer

SETUP_WIZARD_CACHE_KEY = "setup-wizard-keys:v1:"
SETUP_WIZARD_CACHE_TIMEOUT = 600
SETUP_WIZARD_CACHE_EMPTY = "empty"


class SetupWizardView(APIView):
    """
    First step of @sentry/wizard set up. Generates a unique hash for later.
    """

    permission_classes = ()
    throttle_classes = [AnonRateThrottle]

    def delete(self, request, wizard_hash: str = None):
        if wizard_hash is not None:
            key = SETUP_WIZARD_CACHE_KEY + wizard_hash
            cache.delete(key)
        return Response()

    def get(self, request, wizard_hash: str = None):
        if wizard_hash is None:
            wizard_hash = get_random_string(
                64, allowed_chars=string.ascii_lowercase + string.digits
            )
            key = SETUP_WIZARD_CACHE_KEY + wizard_hash
            cache.set(key, SETUP_WIZARD_CACHE_EMPTY, SETUP_WIZARD_CACHE_TIMEOUT)
            return Response({"hash": wizard_hash})
        else:
            key = SETUP_WIZARD_CACHE_KEY + wizard_hash
            wizard_data = cache.get(key)

            if wizard_data is None:
                return Response(status=404)
            elif wizard_data == SETUP_WIZARD_CACHE_EMPTY:
                return Response(status=400)

            return Response(wizard_data)


class SetupWizardSetTokenView(APIView):
    """
    Second step of @sentry/wizard set up
    Use existing unique hash to assign an auth token to the hash based cache key
    """

    serializer_class = SetupWizardSerializer

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        wizard_hash = serializer.data.get("hash")
        key = SETUP_WIZARD_CACHE_KEY + wizard_hash
        wizard_data = cache.get(key)
        if wizard_data is None:
            return Response(status=400)

        organizations = request.user.organizations_ext_organization.all()
        projects = Project.objects.filter(organization__in=organizations)[:50]

        scope = getattr(APIToken.scopes, "project:releases")
        tokens = request.user.apitoken_set.filter(scopes=scope)
        if not tokens:
            token = request.user.apitoken_set.create(scopes=scope)
        else:
            token = tokens[0]

        SetupWizardResult = namedtuple("SetupWizardResult", ("apiKeys", "projects"))
        response_serializer = SetupWizardResultSerializer(
            SetupWizardResult(apiKeys=token, projects=projects),
            context={"request": request},
        )

        cache.set(key, response_serializer.data, SETUP_WIZARD_CACHE_TIMEOUT)
        return Response()
