import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from uuid import UUID

import orjson
from django.db import connection

from glitchtip.partition_manager import UUID7Helper

from .constants import LEVEL_MAP, LogLevel
from .models import compute_hash_bucket

logger = logging.getLogger(__name__)

# Maximum allowed time difference between client timestamp and server time
MAX_TIMESTAMP_DRIFT = timedelta(days=1)


def update_log_statistics(
    stats_data: defaultdict[datetime, defaultdict[tuple[int, int, int, int], dict]],
) -> None:
    """
    Bulk upsert hourly log statistics by project, level, service bucket, and environment bucket.

    stats_data structure: {hour: {(project_id, level, service_bucket, environment_bucket): {"count": N, "organization_id": X}}}
    """
    data = []
    for date, inner_dict in stats_data.items():
        for (
            project_id,
            level,
            service_bucket,
            environment_bucket,
        ), stats in inner_dict.items():
            if (organization_id := stats.get("organization_id")) is not None:
                data.append(
                    [
                        date,
                        project_id,
                        organization_id,
                        level,
                        service_bucket,
                        environment_bucket,
                        stats["count"],
                    ]
                )

    if not data:
        return

    data.sort(key=itemgetter(0, 1, 2, 3, 4, 5))

    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s,%s,%s,%s)", x) for x in data)
        sql = (
            "INSERT INTO projects_logprojecthourlystatistic (date, project_id, organization_id, level, service_bucket, environment_bucket, count)\n"
            f"VALUES {args_str}\n"
            "ON CONFLICT (project_id, organization_id, date, level, service_bucket, environment_bucket)\n"
            "DO UPDATE SET count = projects_logprojecthourlystatistic.count + EXCLUDED.count;"
        )
        cursor.execute(sql)


def update_resource_lookup(resource_data: set[tuple[int, str, str]]) -> None:
    """
    Bulk upsert unique resource names to the lookup table.

    resource_data: set of (organization_id, name, type) tuples
    """
    if not resource_data:
        return

    data = sorted(
        [org_id, name, res_type]
        for org_id, name, res_type in resource_data
        if name
    )

    if not data:
        return

    with connection.cursor() as cursor:
        args_str = ",".join(cursor.mogrify("(%s,%s,%s, NOW(), NOW())", x) for x in data)
        sql = (
            "INSERT INTO logs_logresource (organization_id, name, type, first_seen, last_seen)\n"
            f"VALUES {args_str}\n"
            "ON CONFLICT (organization_id, name, type)\n"
            "DO UPDATE SET last_seen = NOW();"
        )
        cursor.execute(sql)


def validate_timestamp(client_timestamp: datetime, server_time: datetime) -> bool:
    """
    Validate that client timestamp is within acceptable drift from server time.

    Args:
        client_timestamp: Timestamp from client SDK
        server_time: Current server time

    Returns:
        True if timestamp is valid, False if it should be rejected
    """
    drift = abs(client_timestamp - server_time)
    return drift <= MAX_TIMESTAMP_DRIFT


def parse_span_id(span_id_str: str | None) -> int | None:
    """
    Parse span_id from hex string to signed 64-bit integer.

    OpenTelemetry span_id is 8 bytes (16 hex chars), an unsigned value.
    PostgreSQL BIGINT is signed 64-bit (-2^63 to 2^63-1), so we convert
    unsigned values with the high bit set to their signed two's complement.
    """
    if not span_id_str:
        return None
    try:
        clean_str = span_id_str.replace("-", "").replace("0x", "")
        value = int(clean_str, 16)
        # Convert unsigned 64-bit to signed 64-bit for PostgreSQL BIGINT
        if value >= (1 << 63):
            value -= 1 << 64
        return value
    except (ValueError, TypeError):
        return None


def process_log_events(messages: list) -> int:
    """
    Process and bulk insert log events.

    Generates UUIDv7 from client timestamp (not server time), enabling
    time-based queries directly from the id field. Rejects logs with
    timestamps more than 1 day off from server time.

    Args:
        messages: List of LogTaskMessage objects containing log data

    Returns:
        Number of log events inserted
    """
    if not messages:
        return 0

    server_time = datetime.now(timezone.utc)

    # Collect all log events to insert
    log_rows = []
    rejected_count = 0

    # Track statistics by hour, project, level, service bucket, and environment bucket
    project_hourly_stats: defaultdict[
        datetime, defaultdict[tuple[int, int, int, int], dict]
    ] = defaultdict(lambda: defaultdict(lambda: {"count": 0, "organization_id": None}))

    # Track unique resources for lookup table
    unique_resources: set[tuple[int, str, str]] = set()

    for message in messages:
        organization_id = message.organization_id
        project_id = message.project_id

        for log_item in message.logs:
            # Parse timestamp from the log item
            timestamp_val = log_item.get("timestamp")
            if isinstance(timestamp_val, (int, float)):
                # Unix timestamp with fractional seconds
                log_timestamp = datetime.fromtimestamp(timestamp_val, tz=timezone.utc)
            elif isinstance(timestamp_val, str):
                # ISO format string
                log_timestamp = datetime.fromisoformat(
                    timestamp_val.replace("Z", "+00:00")
                )
            else:
                # No timestamp provided - use server time
                log_timestamp = server_time

            # Validate timestamp is within acceptable range
            if not validate_timestamp(log_timestamp, server_time):
                rejected_count += 1
                logger.debug(
                    f"Rejected log with timestamp {log_timestamp} "
                    f"(server time: {server_time}, drift > {MAX_TIMESTAMP_DRIFT})"
                )
                continue

            # Generate UUIDv7 from client timestamp
            # This embeds the timestamp in the id for efficient time-based queries
            log_id = UUID7Helper.from_datetime(log_timestamp)

            # Parse level
            level_str = log_item.get("level", "info").lower()
            level = LEVEL_MAP.get(level_str, LogLevel.INFO)

            # Parse trace_id if present (treat all-zero as NULL)
            trace_id_str = log_item.get("trace_id")
            trace_id = None
            if trace_id_str:
                try:
                    parsed = UUID(trace_id_str)
                    if parsed.int != 0:
                        trace_id = parsed
                except (ValueError, TypeError):
                    pass

            # Parse span_id (convert hex string to integer)
            span_id = parse_span_id(log_item.get("span_id"))

            # Get body and other fields
            body = log_item.get("body", "")
            severity_number = log_item.get("severity_number")

            service = log_item.get("service", "")
            environment = log_item.get("environment", "")
            host = log_item.get("host", "")

            # Build data dict for any extra attributes
            data = {}
            excluded_keys = {
                "timestamp",
                "level",
                "body",
                "trace_id",
                "span_id",
                "severity_number",
                "service",
                "environment",
                "host",
            }
            for key, value in log_item.items():
                if key not in excluded_keys:
                    data[key] = value

            log_rows.append(
                (
                    str(log_id),
                    str(trace_id) if trace_id else None,
                    organization_id,
                    project_id,
                    span_id,
                    level,
                    severity_number,
                    body,
                    service,
                    environment,
                    host,
                    orjson.dumps(data if data else {}).decode("utf-8"),
                )
            )

            # Track statistics - truncate to hour, group by project, level, service bucket, and environment bucket
            hour_received = log_timestamp.replace(minute=0, second=0, microsecond=0)
            service_bucket = compute_hash_bucket(service)
            environment_bucket = compute_hash_bucket(environment)
            stats_key = (project_id, level, service_bucket, environment_bucket)
            project_stats = project_hourly_stats[hour_received][stats_key]
            project_stats["count"] += 1
            project_stats["organization_id"] = organization_id

            # Track unique resources for lookup table
            if service:
                unique_resources.add((organization_id, service, "service"))
            if environment:
                unique_resources.add((organization_id, environment, "environment"))
            if host:
                unique_resources.add((organization_id, host, "host"))

    if rejected_count > 0:
        logger.warning(
            f"Rejected {rejected_count} logs due to timestamp drift > {MAX_TIMESTAMP_DRIFT}"
        )

    if not log_rows:
        return 0

    # Bulk insert using raw SQL for performance
    # Column order matches new schema (no timestamp column)
    insert_sql = """
        INSERT INTO logs_logevent (
            id, trace_id,
            organization_id, project_id, span_id,
            level, severity_number,
            body, service, environment, host, data
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """

    with connection.cursor() as cursor:
        cursor.executemany(insert_sql, log_rows)

    # Update hourly statistics
    update_log_statistics(project_hourly_stats)

    # Update resource name lookup table
    update_resource_lookup(unique_resources)

    logger.info(f"Inserted {len(log_rows)} log events")
    return len(log_rows)
