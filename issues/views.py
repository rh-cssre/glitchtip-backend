from django.db.models.expressions import RawSQL
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, views, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.filters import OrderingFilter

from django_filters.rest_framework import DjangoFilterBackend
from .models import Issue, Event, EventStatus
from .serializers import (
    IssueSerializer,
    EventSerializer,
    EventDetailSerializer,
)
from .filters import IssueFilter
from .permissions import IssuePermission, EventPermission


class IssueViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
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
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    permission_classes = [IssuePermission]
    ordering = ["-last_seen"]
    ordering_fields = ["last_seen", "created", "count", "priority"]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        qs = (
            super()
            .get_queryset()
            .filter(project__team__members__user=self.request.user)
        )

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
                    query_value = query_value.strip('"')

                    if query_name == "is":
                        qs = qs.filter(status=EventStatus.from_string(query_value))
                    elif query_name == "has":
                        qs = qs.filter(event__tags__key__key=query_value)
                    else:
                        qs = qs.filter(
                            event__tags__key__key=query_name,
                            event__tags__value=query_value,
                        )

        if queries:
            # Anything left is full text search
            qs = qs.filter(search_vector=queries)

        if str(self.request.query_params.get("sort")).endswith("priority"):
            # Raw SQL must be added when sorting by priority
            # Inspired by https://stackoverflow.com/a/43788975/443457
            qs = qs.annotate(
                priority=RawSQL(
                    "LOG10(count) + EXTRACT(EPOCH FROM last_seen)/300000", ()
                )
            )

        qs = (
            qs.select_related("project")
            .defer("search_vector")
            .prefetch_related("userreport_set")
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
    permission_classes = [EventPermission]

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
            .filter(issue__project__team__members__user=self.request.user)
        )
        if "issue_pk" in self.kwargs:
            qs = qs.filter(issue=self.kwargs["issue_pk"])
        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                issue__project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(issue__project__slug=self.kwargs["project_slug"],)
        qs = qs.prefetch_related("tags__key")
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

    permission_classes = [EventPermission]

    def get(self, request, org, issue, event, format=None):
        event = get_object_or_404(
            Event,
            pk=event,
            issue__project__organization__slug=org,
            issue__project__team__members__user=self.request.user,
        )
        return Response(event.event_json())
