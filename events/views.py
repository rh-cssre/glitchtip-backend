import json
import logging
import random
import string
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef
from django.db.models.expressions import RawSQL
from django.db.utils import IntegrityError
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework import exceptions, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from sentry_sdk import capture_exception, set_context, set_level

from difs.models import DebugInformationFile
from difs.tasks import difs_run_resolve_stacktrace
from performance.serializers import TransactionEventSerializer
from projects.models import Project
from sentry.utils.auth import parse_auth_header

from .negotiation import IgnoreClientContentNegotiation
from .parsers import EnvelopeParser
from .serializers import (
    EnvelopeHeaderSerializer,
    StoreCSPReportSerializer,
    StoreDefaultSerializer,
    StoreErrorSerializer,
)

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
        # Accept both sentry or glitchtip prefix.
        for k in request.GET.keys():
            if k in ["sentry_key", "glitchtip_key"]:
                return request.GET[k]

        if auth_header := request.META.get(
            "HTTP_X_SENTRY_AUTH", request.META.get("HTTP_AUTHORIZATION")
        ):
            result = parse_auth_header(auth_header)
            return result.get("sentry_key", result.get("glitchtip_key"))

        if isinstance(request.data, list):
            if data_first := next(iter(request.data), None):
                if isinstance(data_first, dict):
                    dsn = urlparse(data_first.get("dsn"))
                    if username := dsn.username:
                        return username
        raise exceptions.NotAuthenticated("Unable to find authentication information")

    def get_project(self, request, project_id):
        sentry_key = BaseEventAPIView.auth_from_request(request)
        difs_subquery = DebugInformationFile.objects.filter(project_id=OuterRef("pk"))
        if isinstance(request.data, list) and len(request.data) > 1:
            data = request.data[2]
        else:
            data = request.data
        try:
            project = (
                Project.objects.filter(id=project_id, projectkey__public_key=sentry_key)
                .annotate(
                    has_difs=Exists(difs_subquery),
                    release_id=RawSQL(
                        "select releases_release.id from releases_release inner join releases_releaseproject on releases_releaseproject.release_id = releases_release.id and releases_releaseproject.project_id=%s where version=%s limit 1",
                        [project_id, data.get("release")],
                    ),
                    environment_id=RawSQL(
                        "select environments_environment.id from environments_environment inner join environments_environmentproject on environments_environmentproject.environment_id = environments_environment.id and environments_environmentproject.project_id=%s where environments_environment.name=%s limit 1",
                        [project_id, data.get("environment")],
                    ),
                )
                .select_related("organization")
                .only(
                    "id",
                    "first_event",
                    "slug",
                    "events_chance",
                    "organization__is_accepting_events",
                    "organization__slug",
                )
                .first()
            )
        except ValidationError as err:
            raise exceptions.AuthenticationFailed({"error": "Invalid api key"}) from err
        if not project:
            if Project.objects.filter(id=project_id).exists():
                raise exceptions.AuthenticationFailed({"error": "Invalid api key"})
            raise exceptions.ValidationError("Invalid project_id: %s" % project_id)
        if (
            not project.organization.is_accepting_events
            or not project.is_accepting_events
        ):
            raise exceptions.Throttled(detail="event rejected due to rate limit")
        return project

    def get_event_serializer_class(self, data=None):
        """Determine event type and return serializer"""
        if data is None:
            data = []
        if "exception" in data and data["exception"]:
            return StoreErrorSerializer
        if "platform" not in data:
            return StoreCSPReportSerializer
        return StoreDefaultSerializer

    def process_event(self, data, request, project):
        set_context("incoming event", data)
        serializer = self.get_event_serializer_class(data)(
            data=data, context={"request": self.request, "project": project}
        )
        try:
            serializer.is_valid(raise_exception=True)
        except exceptions.ValidationError as err:
            set_level("warning")
            capture_exception(err)
            logger.warning("Invalid event %s", serializer.errors)
            return Response()
        event = serializer.save()
        if event.data.get("exception") is not None and project.has_difs:
            difs_run_resolve_stacktrace(event.event_id)
        return Response({"id": event.event_id_hex})


class EventStoreAPIView(BaseEventAPIView):
    def post(self, request, *args, **kwargs):
        if settings.MAINTENANCE_EVENT_FREEZE:
            return Response(
                {
                    "message": "Events are not currently being accepted due to database maintenance."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if settings.EVENT_STORE_DEBUG:
            print(json.dumps(request.data))
        try:
            project = self.get_project(request, kwargs.get("id"))
        except exceptions.AuthenticationFailed as err:
            # Replace 403 status code with 401 to match OSS Sentry
            return Response(err.detail, status=401)
        return self.process_event(request.data, request, project)


class CSPStoreAPIView(EventStoreAPIView):
    pass


class EnvelopeAPIView(BaseEventAPIView):
    parser_classes = [EnvelopeParser]

    def get_serializer_class(self):
        return TransactionEventSerializer

    def post(self, request, *args, **kwargs):
        if settings.MAINTENANCE_EVENT_FREEZE:
            return Response(
                {
                    "message": "Events are not currently being accepted due to database maintenance."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if settings.EVENT_STORE_DEBUG:
            print(json.dumps(request.data))
        project = self.get_project(request, kwargs.get("id"))

        data = request.data
        if len(data) < 2:
            logger.warning("Envelope has no headers %s", data)
            raise exceptions.ValidationError("Envelope has no headers")
        event_header_serializer = EnvelopeHeaderSerializer(data=data.pop(0))
        event_header_serializer.is_valid(raise_exception=True)
        # Multi part envelopes are not yet supported
        message_header = data.pop(0)
        if message_header.get("type") == "transaction":
            serializer = self.get_serializer_class()(
                data=data.pop(0), context={"request": self.request, "project": project}
            )
            try:
                serializer.is_valid(raise_exception=True)
            except exceptions.ValidationError as err:
                logger.warning("Invalid envelope payload", exc_info=True)
                raise err
            try:
                event = serializer.save()
            except IntegrityError as err:
                logger.warning("Duplicate event id", exc_info=True)
                raise exceptions.ValidationError("Duplicate event id") from err
            return Response({"id": event.event_id_hex})
        elif message_header.get("type") == "event":
            event_data = data.pop(0)
            return self.process_event(event_data, request, project)
        elif message_header.get("type") == "session":
            return Response(
                {
                    "message": "Attempted to record a session event, which are not supported. This is safe to ignore. You may be able to suppress this message by disabling auto session tracking in your SDK. See https://gitlab.com/glitchtip/glitchtip-backend/-/issues/206"
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        return Response(status=status.HTTP_501_NOT_IMPLEMENTED)
