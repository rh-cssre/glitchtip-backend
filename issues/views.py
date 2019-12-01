from django.db import IntegrityError
from django.core.exceptions import SuspiciousOperation
from rest_framework import viewsets, status, permissions, exceptions
from rest_framework.views import APIView
from rest_framework.response import Response
from utils.auth import parse_auth_header
from projects.models import Project
from .models import Issue, Event
from .serializers import IssueSerializer, StoreSerializer
from .event_store.error import ErrorEvent


class IssueViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Issue.objects.all()
    serializer_class = IssueSerializer


class EventStoreAPIView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, id: int, format=None):
        sentry_key = EventStoreAPIView.auth_from_request(request)
        project = Project.objects.filter(projectkey__public_key=sentry_key).first()
        if not project:
            raise exceptions.PermissionDenied()
        serializer = StoreSerializer(data=request.data)
        if serializer.is_valid():
            data = serializer.data
            error = ErrorEvent()
            metadata = error.get_metadata(data)
            issue, _ = Issue.objects.get_or_create(
                title=error.get_title(metadata),
                location=error.get_location(metadata),
                project=project,
            )
            try:
                event = Event.objects.create(
                    event_id=data["event_id"],
                    exception=data.get("exception"),
                    level=data["level"],
                    platform=data["platform"],
                    sdk=data["sdk"],
                    release=data["release"],
                    breadcrumbs=data["breadcrumbs"],
                    request=data.get("request"),
                    issue=issue,
                )
            except IntegrityError:
                return Response(
                    {
                        "error": f"An event with the same ID already exists ({data['event_id'].replace('-', '')})"
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            return Response({"id": data["event_id"].replace("-", "")})
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


class MakeSampleErrorView(APIView):
    def get(self, request):
        raise Exception("This is a sample error")
