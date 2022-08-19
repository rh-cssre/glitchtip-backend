import shlex
import uuid

from django.db import connection
from django.db.models.expressions import RawSQL
from django.http import HttpResponseNotFound
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, views, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from events.models import Event

from .filters import IssueFilter
from .models import EventStatus, Issue
from .permissions import EventPermission, IssuePermission
from .serializers import EventDetailSerializer, EventSerializer, IssueSerializer


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
    page_size_query_param = "limit"

    def _get_queryset_base(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        qs = (
            super()
            .get_queryset()
            .filter(project__organization__users=self.request.user)
        )

        if "organization_slug" in self.kwargs:
            qs = qs.filter(
                project__organization__slug=self.kwargs["organization_slug"],
            )
        if "project_slug" in self.kwargs:
            qs = qs.filter(
                project__slug=self.kwargs["project_slug"],
            )

        return qs

    def list(self, request, *args, **kwargs):
        try:
            event_id = uuid.UUID(self.request.GET.get("query", ""))
        except ValueError:
            event_id = None

        if event_id and self.request.user.is_authenticated:
            issues = list(self._get_queryset_base().filter(event__event_id=event_id))
            if issues:
                serializer = IssueSerializer(
                    issues, many=True, context={"matching_event_id": event_id.hex}
                )
                return Response(serializer.data, headers={"X-Sentry-Direct-Hit": "1"})

        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = self._get_queryset_base()

        queries = shlex.split(self.request.GET.get("query", ""))
        # First look for structured queries
        for i, query in enumerate(queries):
            query_part = query.split(":", 1)
            if len(query_part) == 2:
                query_name, query_value = query_part
                query_value = query_value.strip('"')

                if query_name == "is":
                    qs = qs.filter(status=EventStatus.from_string(query_value))
                elif query_name == "has":
                    qs = qs.filter(tags__has_key=query_value)
                else:
                    qs = qs.filter(tags__contains={query_name: [query_value]})
            if len(query_part) == 1:
                search_query = " ".join(queries[i:])
                qs = qs.filter(search_vector=search_query)
                # Search queries must be at end of query string, finished when parsing
                break

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
        queryset = self.filter_queryset(self.get_queryset())
        ids = request.GET.getlist("id")
        if ids:
            queryset = queryset.filter(id__in=ids)
        status = EventStatus.from_string(request.data.get("status"))
        queryset.update(status=status)
        return Response({"status": status.label})

    def serialize_tags(self, rows):
        return [
            {
                "topValues": [
                    {"name": row[1], "value": row[1], "count": row[2], "key": key}
                    for row in rows
                    if row[0] == key
                ],
                "uniqueValues": len([row[2] for row in rows if row[0] == key]),
                "name": key,
                "key": key,
                "totalValues": sum([row[2] for row in rows if row[0] == key]),
            }
            for key in {tup[0] for tup in rows}
        ]

    @action(detail=True, methods=["get"])
    def tags(self, request, pk=None):
        """
        Get statistics about tags
        Filter with query param key=<your key>
        """
        instance = self.get_object()
        keys = tuple(request.GET.getlist("key"))
        with connection.cursor() as cursor:
            if keys:
                query = """
                SELECT key, value, count(*)
                FROM (
                    SELECT (each(tags)).key, (each(tags)).value
                    FROM events_event
                    WHERE issue_id=%s
                )
                AS stat
                WHERE key in %s
                GROUP BY key, value
                ORDER BY count DESC, value
                limit 100;
                """
                cursor.execute(query, [instance.pk, keys])
            else:
                query = """
                SELECT key, value, count(*)
                FROM (
                    SELECT (each(tags)).key, (each(tags)).value
                    FROM events_event
                    WHERE issue_id=%s
                )
                AS stat
                GROUP BY key, value
                ORDER BY count DESC, value
                limit 100;
                """
                cursor.execute(query, [instance.pk])
            rows = cursor.fetchall()

        tags = self.serialize_tags(rows)

        return Response(tags)

    @action(detail=True, methods=["get"], url_path=r"tags/(?P<tag>[-\w]+)")
    def tag_detail(self, request, pk=None, tag=None):
        """
        Get statistics about specified tag
        """
        instance = self.get_object()
        with connection.cursor() as cursor:
            query = """
            SELECT key, value, count(*)
            FROM (
                SELECT (each(tags)).key, (each(tags)).value
                FROM events_event
                WHERE issue_id=%s
            )
            AS stat
            WHERE key=%s
            GROUP BY key, value
            ORDER BY count DESC, value;
            """
            cursor.execute(query, [instance.pk, tag])
            rows = cursor.fetchall()
        tags = self.serialize_tags(rows)
        if not tags:
            raise NotFound()
        return Response(tags[0])


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.filter(issue__isnull=False)
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
            qs = qs.filter(
                issue__project__slug=self.kwargs["project_slug"],
            )
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
        try:
            event = (
                Event.objects.filter(
                    pk=event,
                    issue__project__organization__slug=org,
                    issue__project__team__members__user=self.request.user,
                )
                .distinct()
                .get()
            )
        except Event.DoesNotExist:
            return HttpResponseNotFound()
        return Response(event.event_json())
