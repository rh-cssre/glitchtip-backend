import asyncio
import logging
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib import parse
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Query, Router

from apps.organizations_ext.queryset_utils import get_organization_for_user
from apps.projects.models import LogProjectHourlyStatistic
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import set_pagination_headers
from glitchtip.api.permissions import has_permission
from glitchtip.cold_storage import parse_json_field
from glitchtip.partition_manager import UUID7Helper

from .constants import LogLevel, parse_level_filters
from .models import LogResource, compute_hash_bucket
from .schema import (
    LogEventSchema,
    LogFilterSchema,
    LogResourceSchema,
    LogStatsFilterSchema,
    LogStatsSchema,
)

logger = logging.getLogger(__name__)

router = Router()

# Default time range for queries (prevents unbounded queries)
DEFAULT_LOOKBACK_DAYS = 7

# Days of hot storage before data is archived to cold
HOT_STORAGE_DAYS = settings.GLITCHTIP_LOG_HOT_DAYS


def decode_cursor(cursor_str: str | None) -> UUID | None:
    """Decode cursor string to get position (log UUID)."""
    if not cursor_str:
        return None
    try:
        decoded = b64decode(cursor_str).decode()
        tokens = parse.parse_qs(decoded, keep_blank_values=True)
        position = tokens.get("p", [None])[0]
        if position:
            return UUID(position)
    except (ValueError, TypeError):
        pass
    return None


def encode_cursor(position: UUID) -> str:
    """Encode log UUID as cursor string."""
    querystring = f"p={position}"
    return b64encode(querystring.encode()).decode()


@dataclass
class LogEventRow:
    """Row from query, compatible with LogEventSchema resolvers."""

    id: UUID
    trace_id: UUID | None
    organization_id: int
    project_id: int
    span_id: int | None
    level: int
    severity_number: int | None
    body: str
    service: str
    environment: str
    host: str
    data: dict

    @property
    def timestamp(self) -> datetime:
        """Derive timestamp from UUIDv7 id."""
        return UUID7Helper.extract_datetime(self.id)


def _row_to_log_event(row: tuple) -> LogEventRow:
    """Convert a database row (positional) to LogEventRow."""
    return LogEventRow(
        id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
        trace_id=(
            row[1]
            if isinstance(row[1], UUID)
            else (UUID(str(row[1])) if row[1] else None)
        ),
        organization_id=row[2],
        project_id=row[3],
        span_id=row[4],
        level=row[5],
        severity_number=row[6],
        body=row[7],
        service=row[8],
        environment=row[9],
        host=row[10],
        data=parse_json_field(row[11]),
    )


def _build_hot_where(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    environment: str | None = None,
    host: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    cursor_position: UUID | None = None,
) -> tuple[str, list]:
    """Build WHERE clause and params for hot storage queries."""
    where_clauses = ["organization_id = %s"]
    params: list = [organization_id]

    start_uuid, end_uuid = UUID7Helper.get_range_for_date(start_dt, end_dt)
    where_clauses.append("id >= %s")
    where_clauses.append("id < %s")
    params.extend([str(start_uuid), str(end_uuid)])

    if project_ids:
        placeholders = ",".join(["%s"] * len(project_ids))
        where_clauses.append(f"project_id IN ({placeholders})")
        params.extend(project_ids)

    if level_values:
        placeholders = ",".join(["%s"] * len(level_values))
        where_clauses.append(f"level IN ({placeholders})")
        params.extend(level_values)

    if service:
        where_clauses.append("service = %s")
        params.append(service)

    if environment:
        where_clauses.append("environment = %s")
        params.append(environment)

    if host:
        where_clauses.append("host = %s")
        params.append(host)

    if trace_id:
        where_clauses.append("trace_id = %s")
        params.append(trace_id)

    if query:
        where_clauses.append("body ILIKE %s")
        params.append(f"%{query}%")

    if cursor_position:
        where_clauses.append("id < %s")
        params.append(str(cursor_position))

    return " AND ".join(where_clauses), params


def query_hot_storage(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    environment: str | None = None,
    host: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from hot storage (PostgreSQL partitioned table).

    Uses UUIDv7 range for efficient partition pruning.
    """
    where_sql, params = _build_hot_where(
        organization_id,
        start_dt,
        end_dt,
        project_ids,
        level_values,
        service,
        environment,
        host,
        trace_id,
        query,
        cursor_position,
    )

    sql = f"""
        SELECT id, trace_id, organization_id, project_id, span_id,
               level, severity_number, body, service, environment, host, data
        FROM logs_logevent
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT %s
    """
    params.append(limit)

    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    results = []

    with connections[read_only_db].cursor() as cursor:
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            results.append(_row_to_log_event(row))

    return results


def count_hot_storage(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    environment: str | None = None,
    host: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    max_hits: int = 1000,
) -> int:
    """Bounded count of matching logs in hot storage."""
    where_sql, params = _build_hot_where(
        organization_id,
        start_dt,
        end_dt,
        project_ids,
        level_values,
        service,
        environment,
        host,
        trace_id,
        query,
    )

    sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM logs_logevent
            WHERE {where_sql}
            LIMIT %s
        ) bounded
    """
    params.append(max_hits)

    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    with connections[read_only_db].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def query_cold_storage(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    environment: str | None = None,
    host: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from cold storage (per-org Parquet files via standalone DuckDB).

    Enumerates files individually so a corrupt file doesn't poison the
    entire query. DuckDB runs in-process — no PostgreSQL extension
    required, no connection pooling interaction.
    """
    from .cold_storage import is_duckdb_available

    if not is_duckdb_available():
        return []

    from glitchtip.cold_storage import query_cold_parquet_files

    # Build WHERE clause with DuckDB $N positional parameters
    where_parts = ["organization_id = $1"]
    params: list = [organization_id]

    # Time range via UUIDv7 bounds
    start_uuid, end_uuid = UUID7Helper.get_range_for_date(start_dt, end_dt)
    params.extend([str(start_uuid), str(end_uuid)])
    where_parts.append(f"id >= ${len(params) - 1}")
    where_parts.append(f"id < ${len(params)}")

    if project_ids:
        placeholders = ",".join(
            f"${len(params) + i + 1}" for i in range(len(project_ids))
        )
        params.extend(project_ids)
        where_parts.append(f"project_id IN ({placeholders})")

    if level_values:
        placeholders = ",".join(
            f"${len(params) + i + 1}" for i in range(len(level_values))
        )
        params.extend(level_values)
        where_parts.append(f"level IN ({placeholders})")

    if service:
        params.append(service)
        where_parts.append(f"service = ${len(params)}")

    if environment:
        params.append(environment)
        where_parts.append(f"environment = ${len(params)}")

    if host:
        params.append(host)
        where_parts.append(f"host = ${len(params)}")

    if trace_id:
        try:
            validated_trace = UUID(trace_id)
        except (ValueError, AttributeError):
            pass
        else:
            params.append(str(validated_trace))
            where_parts.append(f"trace_id = ${len(params)}")

    if query:
        params.append(f"%{query}%")
        where_parts.append(f"body ILIKE ${len(params)}")

    if cursor_position:
        params.append(str(cursor_position))
        where_parts.append(f"id < ${len(params)}")

    where_sql = " AND ".join(where_parts)

    params.append(int(limit))
    limit_param = f"${len(params)}"

    select_columns = (
        "id, trace_id, organization_id, project_id, span_id, "
        "level, severity_number, body, service, environment, host, data"
    )

    rows = query_cold_parquet_files(
        organization_id=organization_id,
        table_name="logs_logevent",
        select_columns=select_columns,
        where_sql=where_sql,
        params=params,
        limit_param=limit_param,
    )

    results = [_row_to_log_event(row) for row in rows]
    results.sort(key=lambda r: r.id, reverse=True)
    return results[:limit]


async def query_logs_combined(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    environment: str | None = None,
    host: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from both hot and cold storage.

    When the date range spans both tiers, hot and cold are queried in
    parallel via asyncio.to_thread. Results are merged and sorted by id DESC.
    """
    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=HOT_STORAGE_DAYS)

    kwargs = dict(
        organization_id=organization_id,
        project_ids=project_ids,
        level_values=level_values,
        service=service,
        environment=environment,
        host=host,
        trace_id=trace_id,
        query=query,
        limit=limit,
        cursor_position=cursor_position,
    )

    needs_hot = end_dt > hot_cutoff
    needs_cold = start_dt < hot_cutoff

    if needs_hot and needs_cold:
        # Parallel I/O — hot and cold have disjoint time ranges
        # PG: sync_to_async (Django connection management, async cursors in 6.1)
        # DuckDB: asyncio.to_thread (in-process C library, no Django DB)
        hot_task = sync_to_async(query_hot_storage)(
            start_dt=max(start_dt, hot_cutoff),
            end_dt=end_dt,
            **kwargs,
        )
        cold_task = asyncio.to_thread(
            query_cold_storage,
            start_dt=start_dt,
            end_dt=min(end_dt, hot_cutoff),
            **kwargs,
        )
        hot_results, cold_results = await asyncio.gather(hot_task, cold_task)
        results = hot_results + cold_results
    elif needs_hot:
        results = await sync_to_async(query_hot_storage)(
            start_dt=max(start_dt, hot_cutoff),
            end_dt=end_dt,
            **kwargs,
        )
    elif needs_cold:
        results = await asyncio.to_thread(
            query_cold_storage,
            start_dt=start_dt,
            end_dt=min(end_dt, hot_cutoff),
            **kwargs,
        )
    else:
        results = []

    # Sort combined results by id DESC (most recent first)
    results.sort(key=lambda r: r.id, reverse=True)

    return results[:limit]


def _get_log_from_hot(organization_id: int, log_id: UUID) -> LogEventRow | None:
    """Fetch a single log from hot storage (PostgreSQL)."""
    sql = """
        SELECT id, trace_id, organization_id, project_id, span_id,
               level, severity_number, body, service, environment, host, data
        FROM logs_logevent
        WHERE id = %s AND organization_id = %s
        LIMIT 1
    """
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    with connections[read_only_db].cursor() as cursor:
        cursor.execute(sql, [str(log_id), organization_id])
        row = cursor.fetchone()
        if row:
            return _row_to_log_event(row)
    return None


def _get_log_from_cold(
    organization_id: int, log_id: UUID, log_time: datetime
) -> LogEventRow | None:
    """Fetch a single log from cold storage (DuckDB/Parquet)."""
    from glitchtip.cold_storage import (
        get_cold_storage_backend,
        get_org_cold_storage_path,
    )

    from .cold_storage import (
        get_duckdb_connection,
        get_duckdb_parquet_path,
        is_duckdb_available,
    )

    if not is_duckdb_available():
        return None

    storage = get_cold_storage_backend()
    if not storage:
        return None

    date_str = log_time.strftime("%Y%m%d")
    relative_path = get_org_cold_storage_path(
        "logs_logevent", organization_id, date_str
    )
    parquet_path = get_duckdb_parquet_path(storage, relative_path)

    try:
        duck_conn = get_duckdb_connection(storage)
        try:
            sql = f"""
                SELECT id, trace_id, organization_id, project_id, span_id,
                       level, severity_number, body, service, environment, host, data
                FROM read_parquet('{parquet_path}')
                WHERE id = $1 AND organization_id = $2
                LIMIT 1;
            """
            result = duck_conn.execute(sql, [str(log_id), organization_id])
            row = result.fetchone()
            if row:
                return _row_to_log_event(row)
        finally:
            duck_conn.close()

    except Exception as e:
        error_str = str(e)
        if any(
            msg in error_str
            for msg in ("No files found", "Could not open", "404", "Not Found")
        ):
            pass  # File doesn't exist, log not in cold storage
        else:
            raise

    return None


async def get_log_by_id(organization_id: int, log_id: UUID) -> LogEventRow | None:
    """
    Get a single log by ID, checking both hot and cold storage.

    Uses UUIDv7 timestamp to determine which storage tier to query.
    """
    log_time = UUID7Helper.extract_datetime(log_id)
    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=HOT_STORAGE_DAYS)

    # PG: sync_to_async (Django connection management)
    if log_time > hot_cutoff:
        result = await sync_to_async(_get_log_from_hot)(organization_id, log_id)
        if result:
            return result

    # DuckDB: asyncio.to_thread (in-process, no Django DB)
    return await asyncio.to_thread(
        _get_log_from_cold, organization_id, log_id, log_time
    )


@router.get(
    "organizations/{slug:organization_slug}/logs/",
    response=list[LogEventSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_logs(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[LogFilterSchema],
):
    """
    List log events for an organization with optional filtering.

    Queries hot storage (PostgreSQL) for recent data and cold storage
    (S3 Parquet via DuckDB) for older data seamlessly.

    Supports filtering by:
    - project: List of project IDs
    - level: List of log levels (trace, debug, info, warn, error, fatal)
    - service: Service name (exact match)
    - environment: Environment name (exact match)
    - host: Host name (exact match)
    - traceId: Trace ID for correlation
    - query: Full-text search in log body
    - start/end: Time range filtering (defaults to last 7 days)
    - cursor: Pagination cursor for "load more"
    - limit: Results per page (1-200, default 100)
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    # Determine time range (default to last 7 days)
    now = datetime.now(timezone.utc)
    start_dt = filters.start or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    end_dt = filters.end or now

    level_values = parse_level_filters(filters.level)

    # Decode cursor
    cursor_position = decode_cursor(filters.cursor)
    limit = filters.limit

    query_kwargs = dict(
        organization_id=organization.id,
        start_dt=start_dt,
        end_dt=end_dt,
        project_ids=filters.project,
        level_values=level_values,
        service=filters.service,
        environment=filters.environment,
        host=filters.host,
        trace_id=filters.trace_id,
        query=filters.query,
    )

    # Fetch limit + 1 to detect if there's a next page
    results = await query_logs_combined(
        **query_kwargs,
        limit=limit + 1,
        cursor_position=cursor_position,
    )

    # Check if there's a next page
    has_next = len(results) > limit
    page_results = results[:limit]

    # Bounded count (only on first page to avoid repeated cost)
    if not cursor_position:
        hits = await sync_to_async(count_hot_storage)(**query_kwargs)
    else:
        hits = len(page_results)

    # Build pagination headers
    next_cursor = None
    if has_next and page_results:
        next_cursor = encode_cursor(page_results[-1].id)

    set_pagination_headers(response, request, has_next, next_cursor, hits=hits)

    return page_results


@router.get(
    "organizations/{slug:organization_slug}/logs/{uuid:log_id}/",
    response=LogEventSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_log(
    request: AuthHttpRequest,
    organization_slug: str,
    log_id: UUID,
):
    """Get a single log event by ID (searches both hot and cold storage)."""
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    log_event = await get_log_by_id(organization.id, log_id)

    if not log_event:
        raise Http404("Log event not found")

    return log_event


async def query_log_stats(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service_buckets: list[int] | None = None,
    environment_buckets: list[int] | None = None,
) -> dict:
    """
    Query log statistics from PostgreSQL.

    Returns data grouped by level with hourly counts.
    """
    from django.db.models import Sum
    from django.db.models.functions import TruncHour

    qs = LogProjectHourlyStatistic.objects.filter(
        organization_id=organization_id,
        date__gte=start_dt,
        date__lt=end_dt,
    )

    if project_ids:
        qs = qs.filter(project_id__in=project_ids)

    if level_values:
        qs = qs.filter(level__in=level_values)

    if service_buckets:
        qs = qs.filter(service_bucket__in=service_buckets)

    if environment_buckets:
        qs = qs.filter(environment_bucket__in=environment_buckets)

    # Group by hour and level, sum counts
    qs = (
        qs.annotate(hour=TruncHour("date"))
        .values("hour", "level")
        .annotate(total=Sum("count"))
        .order_by("hour", "level")
    )

    # Build result structure
    hours_set: set[datetime] = set()
    level_data: dict[int, dict[datetime, int]] = {}

    async for row in qs:
        hour = row["hour"]
        level = row["level"]
        total = row["total"]

        hours_set.add(hour)
        if level not in level_data:
            level_data[level] = {}
        level_data[level][hour] = total

    # Sort hours
    intervals = sorted(hours_set)

    # Build series for each level
    series = []
    for level_int in sorted(level_data.keys()):
        level_name = LogLevel(level_int).label
        data = [level_data[level_int].get(hour, 0) for hour in intervals]
        series.append({"name": level_name, "data": data})

    return {"intervals": intervals, "series": series}


@router.get(
    "organizations/{slug:organization_slug}/logs/stats/",
    response=LogStatsSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_log_stats(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[LogStatsFilterSchema],
):
    """
    Get log statistics for an organization.

    Returns hourly counts grouped by level for charting.
    Supports filtering by project and level.
    Time range defaults to last 7 days, max 90 days.
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    # Determine time range (default to last 7 days, max 90 days)
    now = datetime.now(timezone.utc)
    start_dt = filters.start or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    end_dt = filters.end or now

    # Enforce max 90 day range
    max_range = timedelta(days=90)
    if end_dt - start_dt > max_range:
        start_dt = end_dt - max_range

    level_values = parse_level_filters(filters.level)

    # Parse service filters to hash buckets
    service_buckets = None
    if filters.service:
        service_buckets = [compute_hash_bucket(s) for s in filters.service]

    # Parse environment filters to hash buckets
    environment_buckets = None
    if filters.environment:
        environment_buckets = [compute_hash_bucket(e) for e in filters.environment]

    # Query stats
    result = await query_log_stats(
        organization_id=organization.id,
        start_dt=start_dt,
        end_dt=end_dt,
        project_ids=filters.project,
        level_values=level_values,
        service_buckets=service_buckets,
        environment_buckets=environment_buckets,
    )

    return result


@router.get(
    "organizations/{slug:organization_slug}/logs/resources/",
    response=list[LogResourceSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_log_resources(
    request: AuthHttpRequest,
    organization_slug: str,
    resource_type: str | None = None,
):
    """
    List unique resource names (service, environment, host) for an organization.

    Returns resources ordered by last_seen (most recent first).
    Used to populate filter dropdowns in the UI.
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    qs = LogResource.objects.filter(organization=organization)
    if resource_type:
        qs = qs.filter(type=resource_type)

    resources = [
        r
        async for r in qs.order_by("-last_seen").values("name", "type", "last_seen")[
            :100
        ]  # Limit to 100 most recent
    ]

    return resources
