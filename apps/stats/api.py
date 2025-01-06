from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db import connection
from django.http import Http404
from ninja import Query, Router

from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .schema import StatsV2Schema

router = Router()

EVENT_TIME_SERIES_SQL = """
SELECT gs.ts, sum(event_stat.count)
FROM generate_series(%s, %s, %s::interval) gs (ts)
LEFT JOIN projects_issueeventprojecthourlystatistic event_stat
ON event_stat.date >= gs.ts AND event_stat.date < gs.ts +  interval '1 hour'
WHERE event_stat.project_id = ANY(%s) or event_stat is null
GROUP BY gs.ts ORDER BY gs.ts;
"""
TRANSACTION_TIME_SERIES_SQL = """
SELECT gs.ts, sum(transaction_stat.count)
FROM generate_series(%s, %s, %s::interval) gs (ts)
LEFT JOIN projects_transactioneventprojecthourlystatistic transaction_stat
ON transaction_stat.date >= gs.ts AND transaction_stat.date < gs.ts +  interval '1 hour'
WHERE transaction_stat.project_id = ANY(%s) or transaction_stat is null
GROUP BY gs.ts ORDER BY gs.ts;
"""


@sync_to_async
def get_timeseries(category, start, end, interval, project_ids):
    if category == "error":
        with connection.cursor() as cursor:
            cursor.execute(
                EVENT_TIME_SERIES_SQL,
                [start, end, interval, project_ids],
            )
            return cursor.fetchall()
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                TRANSACTION_TIME_SERIES_SQL,
                [start, end, interval, project_ids],
            )
            return cursor.fetchall()


@router.get("organizations/{slug:organization_slug}/stats_v2/")
@has_permission(["org:read", "org:write", "org:admin"])
async def stats_v2(
    request: AuthHttpRequest, organization_slug: str, filters: Query[StatsV2Schema]
):
    """
    Reverse engineered stats v2 endpoint. Endpoint in sentry not documented.
    Appears similar to documented sessions endpoint.
    Used by the Sentry Grafana integration.

    Used to return time series statistics.
    Submit query params start, end, and interval (defaults to 1h)
    Limits results to 1000 intervals. For example if using hours, max days would be 41
    """
    start = filters.start.replace(microsecond=0, second=0, minute=0)
    end = (filters.end + timedelta(hours=1)).replace(microsecond=0, second=0, minute=0)
    field = filters.field
    interval = filters.interval
    category = filters.category
    # Get projects that are authorized, filtered by organization, and selected by user
    # Intentionally separate SQL call to simplify raw SQL
    projects = Project.objects.filter(
        organization__slug=organization_slug,
        organization__users=request.auth.user_id,
    )
    if filters.project:
        projects = projects.filter(pk__in=filters.project)
    project_ids = [id async for id in projects.values_list("id", flat=True)]
    if not project_ids:
        raise Http404()

    series = await get_timeseries(category, start, end, interval, project_ids)

    return {
        "intervals": [row[0].astimezone().replace(microsecond=0).isoformat() for row in series],
        "groups": [
            {
                "series": {field: [row[1] for row in series]},
            }
        ],
    }
