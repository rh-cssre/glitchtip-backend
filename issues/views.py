from django.db.models import Count, Max
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, views
from rest_framework.decorators import action
from rest_framework.response import Response
from projects.models import Project
from .models import Issue, Event, EventStatus
from .serializers import (
    IssueSerializer,
    EventSerializer,
    EventDetailSerializer,
)
from .filters import IssueFilter


class IssueViewSet(viewsets.ModelViewSet):
    """
    View and bulk update issues.

    # Bulk updates

    Submit PUT request to bulk update Issue statuses

    ## Query Parameters

    - id (int) — a list of IDs of the issues to be removed.  This parameter shall be repeated for each issue.
    - query (string) — querystring for structured search. Example: "is:unresolved" searches for status=unresolved.
    """

    queryset = Issue.objects.all()
    serializer_class = IssueSerializer
    filterset_class = IssueFilter

    def get_queryset(self):
        # Optimization, doing this in one query (instead of 2) will result in not using gin index
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        projects = Project.objects.filter(
            organization__users=self.request.user, team__members=self.request.user,
        )
        qs = super().get_queryset().filter(project__in=projects)

        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(project__slug=self.kwargs["project_slug"],)

        queries = self.request.GET.get("query")
        if queries:
            # First look for structured queries
            for query in queries.split():
                query_part = query.split(":", 1)
                if len(query_part) == 2:
                    # Remove query from queries
                    queries = queries.replace(query, "").strip()

                    query_name, query_value = query_part
                    if query_name == "is":
                        qs = qs.filter(status=EventStatus.from_string(query_value))

        if queries:
            # Anything left is full text search
            qs = qs.filter(event__search_vector=queries).distinct()

        qs = qs.annotate(count=Count("event"), lastSeen=Max("event__created"),)

        qs = qs.select_related("project")

        return qs

    def bulk_update(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        ids = request.GET.getlist("id")
        queryset = queryset.filter(id__in=ids)
        status = EventStatus.from_string(request.data.get("status"))
        queryset.update(status=status)
        return Response({"status": status.label})


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer

    def get_serializer_class(self):
        if self.action in ["retrieve", "latest"]:
            return EventDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        qs = (
            super()
            .get_queryset()
            .filter(
                issue__project__organization__users=self.request.user,
                issue__project__team__members=self.request.user,
            )
        )
        if "issue_pk" in self.kwargs:
            qs = qs.filter(issue=self.kwargs["issue_pk"])
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                issue__project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(issue__project__slug=self.kwargs["project_slug"],)
        return qs

    @action(detail=False, methods=["get"])
    def latest(self, request, *args, **kwargs):
        instance = self.get_queryset().first()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class EventJsonView(views.APIView):
    """
    Represents a "raw" view of the event
    Not significantly different from event API view in usage but format is very different.
    Exists mainly for Sentry API compatibility
    """

    def get(self, request, org, issue, event, format=None):
        event = get_object_or_404(
            Event,
            pk=event,
            issue__project__organization__slug=org,
            issue__project__organization__users=self.request.user,
            issue__project__team__members=self.request.user,
        )
        return Response(event.event_json())
