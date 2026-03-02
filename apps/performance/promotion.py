"""
Span promotion and compaction for Performance Monitoring V2.

Promotes span_staging rows to per-org Parquet files, then compacts
chunk files into daily files for efficient analytical queries.
"""

import csv
import logging
import os
import tempfile
import time
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    duckdb_quote_path,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    is_duckdb_available,
)
from glitchtip.partition_manager import UUID7Helper

from .cold_storage import SPAN_PARQUET_COLUMN_TYPES

logger = logging.getLogger(__name__)

TABLE_NAME = "performance_spans"

# Process up to this many rows per organization per invocation.
BATCH_LIMIT_PER_ORG = 100_000


def promote_spans() -> tuple[int, bool]:
    """
    Promote span_staging rows to per-org Parquet files.

    1. Get distinct org_ids with rows older than cutoff (partition-prunable)
    2. For each org, query rows with both id + organization_id filters
       (prunes both RANGE and HASH partitions)
    3. Group by date, write chunk Parquet files per org+date
    4. DELETE consumed rows by exact id + organization_id

    Returns (rows_promoted, truncated) where truncated is True if any org
    hit the per-org batch limit, indicating more rows likely remain.

    Concurrency note: This function is not guarded by its own lock — it
    relies on django-tasks' built-in scheduling lock to prevent overlapping
    runs. If called concurrently (e.g., manual enqueue), duplicate span
    rows may appear in Parquet. This is acceptable: span data is ephemeral
    and duplicates only slightly inflate aggregate metrics.
    """
    if not is_duckdb_available():
        logger.debug("DuckDB not available, skipping span promotion")
        return 0, False

    storage = get_cold_storage_backend()
    if not storage:
        logger.debug("No storage backend, skipping span promotion")
        return 0, False

    from apps.performance.models import SpanStaging

    cutoff = timezone.now() - timedelta(minutes=5)
    # UUID7 with min random bits — everything before this was inserted before cutoff
    cutoff_uuid = UUID7Helper.from_datetime(cutoff)

    # Step 1: Get distinct org_ids. This scans range partitions but the query
    # is lightweight (only reads organization_id column).
    org_ids = list(
        SpanStaging.objects.filter(id__lt=cutoff_uuid)
        .values_list("organization_id", flat=True)
        .distinct()
    )

    if not org_ids:
        return 0, False

    total_promoted = 0
    truncated = False

    # Step 2: Process each org separately — both id and organization_id
    # filters allow PostgreSQL to prune RANGE and HASH partitions.
    for org_id in org_ids:
        rows = list(
            SpanStaging.objects.filter(
                id__lt=cutoff_uuid,
                organization_id=org_id,
            ).values_list(
                "id",
                "organization_id",
                "project_id",
                "transaction_name",
                "span_id",
                "transaction_id",
                "op",
                "description",
                "duration",
                "timestamp",
            )[:BATCH_LIMIT_PER_ORG]
        )

        if not rows:
            continue

        if len(rows) >= BATCH_LIMIT_PER_ORG:
            truncated = True

        # Group rows by date within this org
        date_groups: dict[str, list[tuple]] = {}
        for row in rows:
            ts = row[9]  # timestamp
            date_str = ts.strftime("%Y%m%d") if ts else "unknown"
            date_groups.setdefault(date_str, []).append(row)

        for date_str, group_rows in date_groups.items():
            try:
                chunk_path = _write_chunk_parquet(storage, org_id, date_str, group_rows)
            except Exception:
                logger.error(
                    "Failed to write parquet chunk for org %d date %s",
                    org_id,
                    date_str,
                    exc_info=True,
                )
                continue

            # Delete exactly the promoted rows by ID.
            # Includes organization_id for HASH partition pruning.
            group_uuids = [r[0] for r in group_rows]
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM performance_spanstaging
                        WHERE id = ANY(%s)
                          AND organization_id = %s
                        """,
                        [group_uuids, org_id],
                    )
            except Exception:
                # DELETE failed after chunk was written — remove the chunk
                # to prevent duplicate data on the next promotion run.
                logger.error(
                    "Failed to delete promoted rows for org %d date %s, "
                    "removing chunk to prevent duplicates",
                    org_id,
                    date_str,
                    exc_info=True,
                )
                try:
                    storage.delete(chunk_path)
                except Exception:
                    logger.error(
                        "Failed to remove chunk %s — duplicates may "
                        "exist on next promotion run",
                        chunk_path,
                    )
                continue
            total_promoted += len(group_rows)

    if total_promoted:
        logger.info("Promoted %d span rows to cold storage", total_promoted)
    return total_promoted, truncated


def _write_chunk_parquet(storage, org_id: int, date_str: str, rows: list[tuple]) -> str:
    """Write a chunk Parquet file for a single org+date group via CSV."""
    chunk_ts = f"{time.time_ns()}_{os.getpid()}"
    org_dir = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}/org_{org_id}/{date_str}"
    relative_path = f"{org_dir}/chunk_{chunk_ts}.parquet"
    parquet_path = get_duckdb_parquet_path(storage, relative_path)

    # Ensure directory exists for filesystem storage
    parquet_dir = os.path.dirname(parquet_path)
    if not parquet_path.startswith("s3://"):
        os.makedirs(parquet_dir, exist_ok=True)

    # Write rows to CSV — skip the staging id (index 0)
    columns = list(SPAN_PARQUET_COLUMN_TYPES.keys())
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    ) as f:
        csv_path = f.name
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            ts = row[9]
            writer.writerow(
                [
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    ts.isoformat() if ts else "",
                ]
            )

    try:
        col_spec = ", ".join(
            f"'{c}': '{SPAN_PARQUET_COLUMN_TYPES[c]}'" for c in columns
        )
        duck_conn = get_duckdb_connection(storage)
        try:
            duck_conn.execute(
                f"COPY (SELECT * FROM read_csv("
                f"'{duckdb_quote_path(csv_path)}', "
                f"columns={{{col_spec}}}, header=true)) "
                f"TO '{duckdb_quote_path(parquet_path)}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD);"
            )
        finally:
            duck_conn.close()
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass

    return relative_path


def compact_span_chunks() -> int:
    """
    Compact chunk Parquet files into single daily files per org.

    For each org directory, merges chunk files for completed days
    (before today) into a single sorted Parquet file.

    Returns number of files compacted.
    """
    if not is_duckdb_available():
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    spans_prefix = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}"
    now = timezone.now()
    # Skip the last 2 days to avoid racing with the promotion job,
    # which may still be writing chunks for yesterday's timestamps.
    skip_dates = {
        now.strftime("%Y%m%d"),
        (now - timedelta(days=1)).strftime("%Y%m%d"),
    }
    compacted = 0

    try:
        org_dirs, _ = storage.listdir(spans_prefix)
    except (NotImplementedError, OSError):
        return 0

    for org_dir in org_dirs:
        if not org_dir.startswith("org_"):
            continue

        org_path = f"{spans_prefix}/{org_dir}"
        try:
            date_dirs, flat_files = storage.listdir(org_path)
        except (NotImplementedError, OSError):
            continue

        # Process date subdirectories with chunk files
        for date_dir in date_dirs:
            if date_dir in skip_dates:
                continue  # Don't compact recent chunks

            date_path = f"{org_path}/{date_dir}"
            try:
                _, chunk_files = storage.listdir(date_path)
            except (NotImplementedError, OSError):
                continue

            chunks = [f for f in chunk_files if f.endswith(".parquet")]
            if len(chunks) <= 1:
                continue

            try:
                _compact_date_chunks(storage, org_path, date_dir, date_path, chunks)
                compacted += len(chunks)
            except Exception:
                logger.error("Failed to compact chunks in %s", date_path, exc_info=True)

    if compacted:
        logger.info("Compacted %d span chunk files", compacted)

    return compacted


def _compact_date_chunks(
    storage, org_path: str, date_dir: str, date_path: str, chunks: list[str]
):
    """Compact multiple chunk files into a single daily Parquet file.

    Crash safety: On filesystem, writes to a .tmp file first, then
    atomically renames. A crash mid-write leaves a .tmp file (ignored by
    enumerate_org_parquet_files) and chunks remain intact for the next run.
    On S3, PUT is atomic so no temp file is needed.
    """
    # Build list of chunk paths for DuckDB
    chunk_paths = [
        get_duckdb_parquet_path(storage, f"{date_path}/{chunk}") for chunk in chunks
    ]

    # Output path: org_{id}/{date_str}.parquet (flat file)
    output_relative = f"{org_path}/{date_dir}.parquet"
    output_path = get_duckdb_parquet_path(storage, output_relative)

    # Write to a temp file first, then rename for crash safety.
    # If the process crashes mid-write, the .tmp file is ignored by
    # enumerate_org_parquet_files (doesn't match *.parquet) and chunks
    # remain intact for the next compaction run.
    # S3 PUT is atomic, so no temp file needed there.
    is_s3 = output_path.startswith("s3://")
    write_path = output_path if is_s3 else output_path + ".tmp"

    duck_conn = get_duckdb_connection(storage)
    try:
        paths_list = ", ".join(f"'{duckdb_quote_path(p)}'" for p in chunk_paths)
        duck_conn.execute(f"""
            COPY (
                SELECT * FROM read_parquet([{paths_list}])
                ORDER BY timestamp
            ) TO '{duckdb_quote_path(write_path)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:
        duck_conn.close()

    # Atomic rename on filesystem
    if not is_s3:
        os.rename(write_path, output_path)

    # Delete chunk files and empty directory
    for chunk in chunks:
        try:
            storage.delete(f"{date_path}/{chunk}")
        except Exception:
            logger.warning("Failed to delete chunk %s/%s", date_path, chunk)

    # Try to remove the empty date directory (filesystem only)
    if not output_path.startswith("s3://"):
        try:
            os.rmdir(storage.path(date_path))
        except OSError:
            pass
