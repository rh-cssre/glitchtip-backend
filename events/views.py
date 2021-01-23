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
from sentry_sdk import set_context, capture_exception, set_level
from sentry.utils.auth import parse_auth_header
from projects.models import Project
from performance.serializers import TransactionEventSerializer
from .serializers import (
    StoreDefaultSerializer,
    StoreErrorSerializer,
    StoreCSPReportSerializer,
    EnvelopeHeaderSerializer,
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
    with open("events/test_data/py_hi_event.json") as json_file:
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

    def get_project(self, request, project_id):
        sentry_key = BaseEventAPIView.auth_from_request(request)
        try:
            project = (
                Project.objects.filter(id=project_id, projectkey__public_key=sentry_key)
                .select_related("organization")
                .only("id", "first_event", "organization__is_accepting_events")
                .first()
            )
        except ValidationError as e:
            raise exceptions.AuthenticationFailed({"error": "Invalid api key"})
        if not project:
            if Project.objects.filter(id=project_id).exists():
                raise exceptions.AuthenticationFailed({"error": "Invalid api key"})
            raise exceptions.ValidationError("Invalid project_id: %s" % project_id)
        if not project.organization.is_accepting_events:
            raise exceptions.Throttled(detail="event rejected due to rate limit")
        return project


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
        try:
            project = self.get_project(request, kwargs.get("id"))
        except exceptions.AuthenticationFailed as e:
            # Replace 403 status code with 401 to match OSS Sentry
            return Response(e.detail, status=401)

        set_context("incoming event", request.data)
        serializer = self.get_serializer_class(request.data)(
            data=request.data, context={"request": self.request, "project": project}
        )

        try:
            serializer.is_valid(raise_exception=True)
        except exceptions.ValidationError as e:
            set_level("warning")
            capture_exception(e)
            logger.warning("Invalid event %s", serializer.errors)
            return Response()

        event = serializer.save()
        return Response({"id": event.event_id_hex})


class CSPStoreAPIView(EventStoreAPIView):
    pass


class EnvelopeAPIView(BaseEventAPIView):
    parser_classes = [EnvelopeParser]

    def get_serializer_class(self):
        return TransactionEventSerializer

    def post(self, request, *args, **kwargs):
        if settings.EVENT_STORE_DEBUG:
            print(json.dumps(request.data))
        if (
            settings.THROTTLE_TRANSACTION_EVENTS
            and random.random() < settings.THROTTLE_TRANSACTION_EVENTS
        ):
            raise exceptions.Throttled()
        project = self.get_project(request, kwargs.get("id"))

        data = request.data
        if len(data) < 2:
            raise ValidationError("Envelope has no headers")
        event_header_serializer = EnvelopeHeaderSerializer(data=data.pop(0))
        event_header_serializer.is_valid(raise_exception=True)
        # Multi part envelopes are not yet supported
        data.pop(0)  # Message header isn't used at this time

        serializer = self.get_serializer_class()(
            data=data.pop(0), context={"request": self.request, "project": project}
        )
        serializer.is_valid(raise_exception=True)
        event = serializer.save()
        return Response({"id": event.event_id_hex})
