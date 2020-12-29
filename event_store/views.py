import logging
import json
import uuid
import string
import random
from django.core.exceptions import SuspiciousOperation, ValidationError
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework import permissions, exceptions
from rest_framework.response import Response
from rest_framework.views import APIView
from sentry.utils.auth import parse_auth_header
from projects.models import Project
from .serializers import (
    StoreDefaultSerializer,
    StoreErrorSerializer,
    StoreCSPReportSerializer,
)
from .parsers import EnvelopeParser
from .negotiation import IgnoreClientContentNegotiation


logger = logging.getLogger(__name__)


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


class BaseEventAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    content_negotiation_class = IgnoreClientContentNegotiation
    http_method_names = ["post"]

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


class EventStoreAPIView(BaseEventAPIView):
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
        try:
            project = (
                Project.objects.filter(
                    id=kwargs.get("id"), projectkey__public_key=sentry_key
                )
                .select_related("organization")
                .only("id", "first_event", "organization__is_accepting_events")
                .first()
            )
        except ValidationError as e:
            return Response({"error": "Invalid api key"}, status=401)
        if not project:
            if Project.objects.filter(id=kwargs.get("id")).exists():
                return Response({"error": "Invalid api key"}, status=401)
            raise exceptions.ValidationError(
                "Invalid project_id: %s" % kwargs.get("id")
            )
        if not project.organization.is_accepting_events:
            raise exceptions.Throttled(detail="event rejected due to rate limit")
        serializer = self.get_serializer_class(request.data)(
            data=request.data, context={"request": self.request, "project": project}
        )
        if serializer.is_valid():
            event = serializer.save()
            return Response({"id": event.event_id_hex})
        else:
            logger.warning("Invalid event %s", serializer.errors)
        return Response()


class CSPStoreAPIView(EventStoreAPIView):
    pass


class EnvelopeAPIView(BaseEventAPIView):
    parser_classes = [EnvelopeParser]

    def get_serializer_class(self, data):
        pass

    def post(self, request, *args, **kwargs):
        sentry_key = EnvelopeAPIView.auth_from_request(request)
        print(sentry_key)
        print(request.data)
        print(request.META)
        return Response()
