from django.db.models import Count, Min, Max
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Issue, Event, EventStatus
from .serializers import (
    IssueSerializer,
    EventSerializer,
    EventDetailSerializer,
)


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
    filterset_fields = ["project"]

    def get_queryset(self):
        qs = super().get_queryset()
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(project__slug=self.kwargs["project_slug"],)

        queries = self.request.GET.get("query")
        if queries:
            for query in queries.split():
                query_part = query.split(":", 1)
                if len(query_part) == 2:
                    query_name, query_value = query_part
                    if query_name == "is":
                        qs = qs.filter(status=EventStatus.from_string(query_value))

        qs = qs.annotate(
            count=Count("event"),
            firstSeen=Min("event__created"),
            lastSeen=Max("event__created"),
        )
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

    @action(detail=False, methods=["get"])
    def latest(self, request, *args, **kwargs):
        instance = self.get_queryset().first()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
