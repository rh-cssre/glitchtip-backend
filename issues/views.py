from django.core.exceptions import SuspiciousOperation
from django.db.models import Count, Min, Max
from rest_framework import viewsets, permissions, exceptions
from rest_framework.views import APIView
from rest_framework.response import Response
from utils.auth import parse_auth_header
from projects.models import Project
from .models import Issue, Event
from .serializers import (
    IssueSerializer,
    EventSerializer,
    StoreDefaultSerializer,
    StoreErrorSerializer,
    StoreCSPReportSerializer,
)


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.all()
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(project__slug=self.kwargs["project_slug"],)
        qs = qs.annotate(
            count=Count("event"),
            firstSeen=Min("event__created"),
            lastSeen=Max("event__created"),
        )
        return qs


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if "issue_pk" in self.kwargs:
            qs = qs.filter(issue=self.kwargs["issue_pk"])
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                issue__project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(issue__project__slug=self.kwargs["project_slug"],)
        return qs


class EventStoreAPIView(APIView):
    permission_classes = [permissions.AllowAny]

    def get_serializer(self, data):
        """ Determine event type and return serializer """
        if "exception" in data:
            return StoreErrorSerializer(data=data)
        if "platform" not in data:
            return StoreCSPReportSerializer(data=data)
        return StoreDefaultSerializer(data=data)

    def post(self, request, *args, **kwargs):
        sentry_key = EventStoreAPIView.auth_from_request(request)
        project = Project.objects.filter(
            id=kwargs.get("id"), projectkey__public_key=sentry_key
        ).first()
        if not project:
            raise exceptions.PermissionDenied()
        serializer = self.get_serializer(request.data)
        if serializer.is_valid():
            data = serializer.data
            serializer.create(project, serializer.data)
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
