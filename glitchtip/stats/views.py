from datetime import timedelta

from django.db import connection
from rest_framework import views
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND

from organizations_ext.permissions import OrganizationPermission
from projects.models import Project

from .serializers import StatsV2Serializer

EVENT_TIME_SERIES_SQL = """
SELECT gs.ts, count(event.created)
FROM generate_series(%s, %s, %s::interval) gs (ts)
LEFT JOIN events_event event
ON event.created >= gs.ts AND event.created < gs.ts +  interval '1 hour'
LEFT JOIN issues_issue issue
ON event.issue_id = issue.id or event is null
WHERE issue.project_id IN %s
GROUP BY gs.ts ORDER BY gs.ts;
"""


class StatsV2View(views.APIView):
    """
    Reverse engineered stats v2 endpoint. Endpoint in sentry not documented.
    Appears similar to documented sessions endpoint.
    Used by the Sentry Grafana integration.

    Used to return time series statistics.
    Submit query params start, end, and interval (defaults to 1h)
    Limits results to 1000 intervals. For example if using hours, max days would be 41
    """

    permission_classes = [OrganizationPermission]

    def get(self, *args, **kwargs):
        query_params = self.request.query_params
        data = {
            "category": query_params.get("category"),
            "project": query_params.getlist("project"),
            "field": query_params.get("field"),
            "start": query_params.get("start"),
            "end": query_params.get("end"),
        }
        if query_params.get("interval"):
            data["interval"] = query_params.get("interval")
        serializer = StatsV2Serializer(data=data)
        serializer.is_valid(raise_exception=True)

        category = serializer.validated_data["category"]
        start = serializer.validated_data["start"].replace(
            microsecond=0, second=0, minute=0
        )
        end = (serializer.validated_data["end"] + timedelta(hours=1)).replace(
            microsecond=0, second=0, minute=0
        )
        field = serializer.validated_data["field"]
        interval = serializer.validated_data["interval"]
        # Get projects that are authorized, filtered by organization, and selected by user
        # Intentionally separate SQL call to simplify raw SQL
        projects = Project.objects.filter(
            organization__slug=self.kwargs.get("organization_slug"),
            organization__users=self.request.user,
        )
        if serializer.validated_data.get("project"):
            projects = projects.filter(pk__in=serializer.validated_data["project"])
        project_ids = tuple(projects.values_list("id", flat=True))
        if not project_ids:
            return Response(status=HTTP_404_NOT_FOUND)

        if category == "error":
            with connection.cursor() as cursor:
                cursor.execute(
                    EVENT_TIME_SERIES_SQL, [start, end, interval, project_ids],
                )
                series = cursor.fetchall()
        else:
            return Response(status=HTTP_400_BAD_REQUEST)

        data = {
            "intervals": [row[0] for row in series],
            "groups": [{"series": {field: [row[1] for row in series]},}],
        }

        return Response(data)
