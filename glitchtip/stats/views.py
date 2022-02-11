# from django.db.models import
from datetime import timedelta
from dateutil import parser
from django.db import connection
from django.utils.timezone import make_aware
from rest_framework import views
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST


EVENT_TIME_SERIES_SQL = """
SELECT gs.ts, count(event.created)
FROM generate_series(%s, %s, '1 hour'::interval) gs (ts)
LEFT JOIN events_event event
ON event.created >= gs.ts AND event.created < gs.ts +  interval '1 hour'
RIGHT JOIN issues_issue issue
ON event.issue_id = issue.id or event is null
"""

WHERE = "WHERE issue.project_id IN (%s) "

GROUP_BY = "GROUP BY gs.ts ORDER BY gs.ts;"


class StatsV2View(views.APIView):
    """
    Reverse engineered stats v2 endpoint.
    This endpoint in sentry is not open source and not documented, so good luck

    Used by the Sentry Grafana integration.
    """

    def get(self, *args, **kwargs):
        category = self.request.query_params.get("category")
        start = parser.parse(self.request.query_params.get("start")).replace(
            microsecond=0, second=0, minute=0
        )
        end = (
            parser.parse(self.request.query_params.get("end")) + timedelta(hours=1)
        ).replace(microsecond=0, second=0, minute=0)
        field = self.request.query_params.get("field")
        projects = self.request.query_params.getlist("project")
        if projects:
            projects = [int(project) for project in projects][0]
        # ADD permissions next
        print(projects)

        if category == "error":
            with connection.cursor() as cursor:
                if projects:
                    cursor.execute(
                        EVENT_TIME_SERIES_SQL + WHERE + GROUP_BY,
                        [start, end, projects],
                    )
                else:
                    cursor.execute(
                        EVENT_TIME_SERIES_SQL + GROUP_BY, [start, end],
                    )
                series = cursor.fetchall()
        else:
            return Response(status=HTTP_400_BAD_REQUEST)

        data = {
            "intervals": [make_aware(row[0]) for row in series],
            "groups": [{"series": {"sum(quantity)": [row[1] for row in series]},}],
        }

        return Response(data)
