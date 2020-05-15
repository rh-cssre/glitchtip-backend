import json
import uuid
import string
import random
from django.core.exceptions import SuspiciousOperation
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework import permissions, exceptions
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView
from sentry.utils.auth import parse_auth_header
from projects.models import Project
from .serializers import (
    StoreDefaultSerializer,
    StoreErrorSerializer,
    StoreCSPReportSerializer,
)


class IgnoreClientContentNegotiation(BaseContentNegotiation):
    """
    @sentry/browser sends an interesting content-type of text/plain when it's actually sending json
    We have to ignore it and assume it's actually JSON
    """

    def select_parser(self, request, parsers):
        """
        Select the first parser in the `.parser_classes` list.
        """
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix):
        """
        Select the first renderer in the `.renderer_classes` list.
        """
        return (renderers[0], renderers[0].media_type)


def test_event_view(request):
    """
    This view is used only to test event store performance
    It requires DEBUG to be True
    """
    factory = RequestFactory()
    request = request = factory.get(
        "/api/6/store/?sentry_key=244703e8083f4b16988c376ea46e9a08"
    )
    with open("event_store/test_data/py_hi_event.json") as json_file:
        data = json.load(json_file)
    data["event_id"] = uuid.uuid4()
    data["message"] = "".join(
        random.choices(string.ascii_uppercase + string.digits, k=8)
    )
    request.data = data
    EventStoreAPIView().post(request, id=6)

    return HttpResponse("<html><body></body></html>")


class EventStoreAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    content_negotiation_class = IgnoreClientContentNegotiation
    http_method_names = ["post"]

    def get_serializer_class(self, data=[]):
        """ Determine event type and return serializer """
        if "exception" in data and data["exception"]:
            return StoreErrorSerializer
        if "platform" not in data:
            return StoreCSPReportSerializer
        return StoreDefaultSerializer

    def post(self, request, *args, **kwargs):
        if settings.EVENT_STORE_DEBUG:
            print(json.dumps(request.data))
        sentry_key = EventStoreAPIView.auth_from_request(request)
        project = (
            Project.objects.filter(
                id=kwargs.get("id"), projectkey__public_key=sentry_key
            )
            .select_related("organization")
            .only("id", "organization__is_accepting_events")
            .first()
        )
        if not project.organization.is_accepting_events:
            raise exceptions.Throttled(detail="event rejected due to rate limit")
        if not project:
            raise exceptions.PermissionDenied()
        serializer = self.get_serializer_class(request.data)(data=request.data)
        if serializer.is_valid():
            event = serializer.create(project.id, serializer.data)
            return Response({"id": event.event_id_hex})
        # TODO {"error": "Invalid api key"}, CSP type, valid json but no type at all
        return Response()

    @classmethod
    def auth_from_request(cls, request):
        result = {k: request.GET[k] for k in request.GET.keys() if k[:7] == "sentry_"}

        if request.META.get("HTTP_X_SENTRY_AUTH", "")[:7].lower() == "sentry ":
            if result:
                raise SuspiciousOperation(
                    "Multiple authentication payloads were detected."
                )
            result = parse_auth_header(request.META["HTTP_X_SENTRY_AUTH"])
        elif request.META.get("HTTP_AUTHORIZATION", "")[:7].lower() == "sentry ":
            if result:
                raise SuspiciousOperation(
                    "Multiple authentication payloads were detected."
                )
            result = parse_auth_header(request.META["HTTP_AUTHORIZATION"])

        if not result:
            raise exceptions.NotAuthenticated(
                "Unable to find authentication information"
            )

        return result.get("sentry_key")


class CSPStoreAPIView(EventStoreAPIView):
    pass
