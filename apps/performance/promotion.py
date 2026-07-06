"""
Span promotion and compaction for Performance Monitoring V2.

Promotes span_staging rows to per-org Parquet files, then compacts
chunk files into daily files for efficient analytical queries.
"""

import io
import logging
import os
import time
from datetime import UTC, datetime, timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.utils import timezone

from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    _is_s3_storage,
    _parquet_encoding_opts,
    duckdb_quote_path,
    duckdb_slot,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    is_duckdb_available,
)
from glitchtip.partition_manager import UUID7Helper

from .cold_storage import (
    ROLLUP_STORAGE_PREFIX,
    SPAN_PARQUET_COLUMN_TYPES,
    STORAGE_PREFIX,
)

logger = logging.getLogger(__name__)

# Process up to this many rows per organization per invocation.
BATCH_LIMIT_PER_ORG = 100_000

# An hour is sealed (safe to compact, no longer accepting chunks) once this
# long after the hour ends. Only throttles re-compaction churn — compaction
# is idempotent — so it just needs to exceed normal promotion/queue lag.
# A span arriving later than this for an already-sealed hour is dropped.
SEAL_GRACE = timedelta(minutes=20)


def _delete_promoted_rows(group_uuids: list, org_id: int) -> None:
    """
    Delete promoted staging rows by id + organization_id.

    Uses raw SQL with ``id = ANY(%s)`` rather than ORM ``.delete()`` with
    ``id__in=...``. The ORM path inlines every UUID as a SQL literal
    (tens of KB of query text per batch) and wraps the statement in
    BEGIN/COMMIT. ``ANY(%s)`` passes the UUID list as a single bound
    array parameter, skipping the parse/plan blowup on large batches.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM performance_spanstaging
            WHERE id = ANY(%s)
              AND organization_id = %s
            """,
            [group_uuids, org_id],
        )


async def promote_spans() -> tuple[int, bool]:
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

    now_dt = timezone.now()
    cutoff = now_dt - timedelta(minutes=5)
    # UUID7 with min random bits — everything before this was inserted before cutoff
    cutoff_uuid = UUID7Helper.from_datetime(cutoff)

    # Garbage guard (not a correctness mechanism — compaction is idempotent
    # and seals by ingestion time). Spans with a missing timestamp, a
    # far-future timestamp, or one already past raw retention are not worth
    # a Parquet chunk: they are consumed (deleted from staging) and dropped.
    min_ts = now_dt - timedelta(days=settings.GLITCHTIP_SPAN_RAW_RETENTION_DAYS)
    max_ts = now_dt + settings.GLITCHTIP_TRANSACTION_FUTURE_SKEW

    # Step 1: Get distinct org_ids. This scans range partitions but the query
    # is lightweight (only reads organization_id column).
    org_ids = [
        org_id
        async for org_id in SpanStaging.objects.filter(id__lt=cutoff_uuid)
        .values_list("organization_id", flat=True)
        .distinct()
    ]

    if not org_ids:
        return 0, False

    total_promoted = 0
    total_dropped = 0
    truncated = False

    # Step 2: Process each org separately — both id and organization_id
    # filters allow PostgreSQL to prune RANGE and HASH partitions.
    for org_id in org_ids:
        rows = [
            row
            async for row in SpanStaging.objects.filter(
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
        ]

        if not rows:
            continue

        if len(rows) >= BATCH_LIMIT_PER_ORG:
            truncated = True

        # Group rows by (date, hour) — the T1 raw unit. Garbage spans
        # (missing / far-future / past-retention timestamps) are dropped but
        # still consumed below so they don't reaccumulate in staging.
        hour_groups: dict[tuple[str, str], list[tuple]] = {}
        drop_uuids: list = []
        for row in rows:
            ts = row[9]  # timestamp
            if ts is None or ts < min_ts or ts > max_ts:
                drop_uuids.append(row[0])
                continue
            key = (ts.strftime("%Y%m%d"), ts.strftime("%H"))
            hour_groups.setdefault(key, []).append(row)

        for (date_str, hour_str), group_rows in hour_groups.items():
            try:
                chunk_path = await _write_chunk_parquet(
                    storage, org_id, date_str, hour_str, group_rows
                )
            except Exception:
                logger.error(
                    "Failed to write parquet chunk for org %d %s/%s",
                    org_id,
                    date_str,
                    hour_str,
                    exc_info=True,
                )
                continue

            # Delete exactly the promoted rows by ID.
            # Includes organization_id for HASH partition pruning.
            group_uuids = [r[0] for r in group_rows]
            try:
                await sync_to_async(_delete_promoted_rows)(group_uuids, org_id)
            except Exception:
                # DELETE failed after chunk was written — remove the chunk
                # to prevent duplicate data on the next promotion run.
                logger.error(
                    "Failed to delete promoted rows for org %d %s/%s, "
                    "removing chunk to prevent duplicates",
                    org_id,
                    date_str,
                    hour_str,
                    exc_info=True,
                )
                try:
                    await sync_to_async(storage.delete)(chunk_path)
                except Exception:
                    logger.error(
                        "Failed to remove chunk %s — duplicates may "
                        "exist on next promotion run",
                        chunk_path,
                    )
                continue
            total_promoted += len(group_rows)

        # Consume dropped rows so they don't reaccumulate in staging.
        # Non-fatal on failure — they'll be retried next run.
        if drop_uuids:
            try:
                await sync_to_async(_delete_promoted_rows)(drop_uuids, org_id)
                total_dropped += len(drop_uuids)
            except Exception:
                logger.warning(
                    "Failed to delete %d dropped span rows for org %d",
                    len(drop_uuids),
                    org_id,
                    exc_info=True,
                )

    if total_promoted:
        logger.info("Promoted %d span rows to cold storage", total_promoted)
    if total_dropped:
        logger.info("Dropped %d whacky/stale span rows during promotion", total_dropped)
    return total_promoted, truncated


async def _write_chunk_parquet(
    storage, org_id: int, date_str: str, hour_str: str, rows: list[tuple]
) -> str:
    """Write a chunk Parquet file for a single org+date+hour group via arro3.

    Builds Arrow arrays directly from Python tuples — no CSV serialization,
    no temp files, no DuckDB dependency for writes. arro3 and Django
    storage are sync-only today; ``sync_to_async`` is applied at each leaf
    call so the surrounding task stays async-native.

    The ``time.time_ns()`` filename prefix gives a monotonic ingestion
    sequence used by compaction to seal an hour idempotently.
    """
    import arro3.core as ac
    import arro3.io as aio

    chunk_ts = f"{time.time_ns()}_{os.getpid()}"
    hour_dir = (
        f"{COLD_STORAGE_PREFIX}/{STORAGE_PREFIX}/org_{org_id}/{date_str}/{hour_str}"
    )
    relative_path = f"{hour_dir}/chunk_{chunk_ts}.parquet"

    # Build Arrow arrays directly from row tuples.
    # Row layout: (id, org_id, project_id, txn_name, span_id, txn_id,
    #              op, description, duration, timestamp)
    batch = ac.RecordBatch.from_arrays(
        [
            ac.Array([r[1] for r in rows], type=ac.DataType.int32()),
            ac.Array([r[2] for r in rows], type=ac.DataType.int32()),
            ac.Array([r[3] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[4] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[5] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[6] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[7] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[8] for r in rows], type=ac.DataType.float64()),
            # arro3 doesn't yet support timestamp from Python lists;
            # build as int64 microseconds then cast.
            ac.Array(
                [int(r[9].timestamp() * 1_000_000) if r[9] else 0 for r in rows],
                type=ac.DataType.int64(),
            ).cast(ac.DataType.timestamp("us")),
        ],
        names=list(SPAN_PARQUET_COLUMN_TYPES.keys()),
    )

    encoding_opts = _parquet_encoding_opts(SPAN_PARQUET_COLUMN_TYPES)
    write_kwargs = {
        "compression": "zstd(3)",
        "max_row_group_size": min(len(rows), 100_000),
        **encoding_opts,
    }

    if _is_s3_storage(storage):
        buf = io.BytesIO()
        await sync_to_async(aio.write_parquet)(batch, buf, **write_kwargs)
        buf.seek(0)
        from django.core.files.base import ContentFile

        try:
            await sync_to_async(storage.delete)(relative_path)
        except Exception:
            pass
        await sync_to_async(storage.save)(relative_path, ContentFile(buf.read()))
    else:
        parquet_path = storage.path(relative_path)
        await sync_to_async(os.makedirs)(os.path.dirname(parquet_path), exist_ok=True)
        await sync_to_async(aio.write_parquet)(batch, parquet_path, **write_kwargs)

    return relative_path


def _delete_subtree(storage, rel: str) -> None:
    """Recursively delete every object under a storage-relative directory."""
    try:
        dirs, files = storage.listdir(rel)
    except (NotImplementedError, OSError):
        return
    for f in files:
        try:
            storage.delete(f"{rel}/{f}")
        except Exception:
            logger.warning("Failed to delete %s/%s", rel, f)
    for d in dirs:
        _delete_subtree(storage, f"{rel}/{d}")
    try:
        os.rmdir(storage.path(rel))
    except (OSError, NotImplementedError):
        pass


def _duckdb_copy(storage, input_relpaths: list[str], output_relpath: str) -> bool:
    """COPY many Parquet inputs into one output via DuckDB.

    Memory is bounded by ``DUCKDB_MEMORY_LIMIT`` + ``ROW_GROUP_SIZE`` and the
    process-wide ``duckdb_slot`` (benchmarked equivalent to arro3 here, with
    far less code — see scripts/bench_compaction_ab.py). Filesystem writes go
    via a ``.tmp`` rename; S3 PUT is atomic. Returns False if no slot was
    free (caller retries next run; inputs are left intact).
    """
    in_paths = [get_duckdb_parquet_path(storage, p) for p in input_relpaths]
    out_path = get_duckdb_parquet_path(storage, output_relpath)
    is_s3 = out_path.startswith("s3://")
    write_path = out_path if is_s3 else out_path + ".tmp"

    with duckdb_slot() as slot:
        if not slot:
            return False
        conn = get_duckdb_connection(storage)
        try:
            paths_list = ", ".join(f"'{duckdb_quote_path(p)}'" for p in in_paths)
            conn.execute(
                f"COPY (SELECT * FROM read_parquet([{paths_list}])) "
                f"TO '{duckdb_quote_path(write_path)}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)"
            )
        finally:
            conn.close()

    if not is_s3:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        os.rename(write_path, out_path)
    return True


def _write_hour_rollup(
    storage, hour_file_rel: str, rollup_rel: str, hour_start: datetime
) -> None:
    """Aggregate one sealed hour file into its tiny rollup (T3).

    DuckDB only *reads* and produces a small grouped result (one row per
    (project, transaction, op, description) — sized by cardinality, not span
    count); the write itself goes through arro3, consistent with the
    arro3-for-writes rule. Idempotent: overwrites the hour rollup.
    """
    src = get_duckdb_parquet_path(storage, hour_file_rel)
    with duckdb_slot() as slot:
        if not slot:
            return
        conn = get_duckdb_connection(storage)
        try:
            rows = conn.execute(
                f"""
                SELECT project_id, transaction_name, op, description,
                    COUNT(*), SUM(duration), MIN(duration), MAX(duration),
                    approx_quantile(duration, 0.5),
                    approx_quantile(duration, 0.95),
                    COUNT(DISTINCT transaction_id)
                FROM read_parquet('{duckdb_quote_path(src)}')
                GROUP BY project_id, transaction_name, op, description
                """
            ).fetchall()
        finally:
            conn.close()
    if not rows:
        return

    import arro3.core as ac
    import arro3.io as aio

    bucket_us = int(hour_start.timestamp() * 1_000_000)
    batch = ac.RecordBatch.from_arrays(
        [
            ac.Array([r[0] for r in rows], type=ac.DataType.int32()),
            ac.Array([r[1] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[2] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[3] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[4] for r in rows], type=ac.DataType.int64()),
            ac.Array([r[5] or 0.0 for r in rows], type=ac.DataType.float64()),
            ac.Array([r[6] or 0.0 for r in rows], type=ac.DataType.float64()),
            ac.Array([r[7] or 0.0 for r in rows], type=ac.DataType.float64()),
            ac.Array([r[8] or 0.0 for r in rows], type=ac.DataType.float64()),
            ac.Array([r[9] or 0.0 for r in rows], type=ac.DataType.float64()),
            ac.Array([r[10] for r in rows], type=ac.DataType.int64()),
            ac.Array([bucket_us] * len(rows), type=ac.DataType.int64()).cast(
                ac.DataType.timestamp("us")
            ),
        ],
        names=[
            "project_id",
            "transaction_name",
            "op",
            "description",
            "count",
            "sum_duration",
            "min_duration",
            "max_duration",
            "p50",
            "p95",
            "transaction_count",
            "hour_bucket",
        ],
    )
    write_kwargs = {"compression": "zstd(3)", "max_row_group_size": 100_000}
    if _is_s3_storage(storage):
        from django.core.files.base import ContentFile

        buf = io.BytesIO()
        aio.write_parquet(batch, buf, **write_kwargs)
        buf.seek(0)
        try:
            storage.delete(rollup_rel)
        except Exception:
            pass
        storage.save(rollup_rel, ContentFile(buf.read()))
    else:
        path = storage.path(rollup_rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        aio.write_parquet(batch, path, **write_kwargs)


def compact_span_chunks() -> int:
    """
    Collapse the raw span tiers and emit trend rollups.

    Tiers (per org):

    - **Open hour:** ``{date}/{HH}/chunk_*.parquet`` — promotion appends here.
    - **Sealed hour:** ``{date}/{HH}.parquet`` — one file per completed hour.
    - **Sealed day:** ``{date}.parquet`` — lazy roll of a fully-sealed day.
    - **Rollup:** ``performance_spans_rollup/...`` — small per-group hourly
      aggregates for trend queries, kept far longer than raw.

    An hour is sealed ``SEAL_GRACE`` after it ends. Sealing is idempotent
    and never rebuilds an existing sealed file, so a span arriving for an
    already-sealed hour is dropped (acceptably rare with correct-ish clocks;
    matches the accepted "duplicates only slightly skew aggregates" stance).
    Cheap to run often — most calls find nothing newly sealed.

    Returns number of chunk files compacted into sealed hours.
    """
    if not is_duckdb_available():
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    raw_prefix = f"{COLD_STORAGE_PREFIX}/{STORAGE_PREFIX}"
    rollup_prefix = f"{COLD_STORAGE_PREFIX}/{ROLLUP_STORAGE_PREFIX}"
    now = timezone.now()
    compacted = 0

    try:
        org_dirs, _ = storage.listdir(raw_prefix)
    except (NotImplementedError, OSError):
        return 0

    for org_dir in org_dirs:
        if not org_dir.startswith("org_"):
            continue
        raw_org = f"{raw_prefix}/{org_dir}"
        rollup_org = f"{rollup_prefix}/{org_dir}"
        try:
            date_dirs, _ = storage.listdir(raw_org)
        except (NotImplementedError, OSError):
            continue

        for date_dir in date_dirs:
            try:
                day_start = datetime.strptime(date_dir, "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                continue  # not a date dir
            try:
                compacted += _compact_org_date(
                    storage, raw_org, rollup_org, date_dir, day_start, now
                )
            except Exception:
                logger.error(
                    "Failed compacting %s/%s", raw_org, date_dir, exc_info=True
                )

    if compacted:
        logger.info("Compacted %d span chunk files", compacted)
    return compacted


def _compact_org_date(
    storage, raw_org: str, rollup_org: str, date_dir: str, day_start, now
) -> int:
    """Seal due hours for one org+date, then lazily roll the sealed day."""
    date_path = f"{raw_org}/{date_dir}"
    try:
        hour_dirs, hour_files = storage.listdir(date_path)
    except (NotImplementedError, OSError):
        hour_dirs, hour_files = [], []

    # Recover any sealed hour that's missing its rollup. The rollup write
    # happens after the seal and can fail (slot saturated, transient S3,
    # etc.); without recovery a single failure silently drops that hour
    # from every trend query until raw retention expires. We re-derive the
    # rollup from the sealed raw file — cheap and idempotent.
    for hf in hour_files:
        if not hf.endswith(".parquet"):
            continue
        hh = hf.removesuffix(".parquet")
        if len(hh) != 2 or not hh.isdigit():
            continue
        rollup_file = f"{rollup_org}/{date_dir}/{hh}.parquet"
        if storage.exists(rollup_file):
            continue
        hour_start = day_start + timedelta(hours=int(hh))
        try:
            _write_hour_rollup(storage, f"{date_path}/{hf}", rollup_file, hour_start)
        except Exception:
            logger.error("Failed recovery rollup %s/%s", date_dir, hh, exc_info=True)

    compacted = 0
    for hh in hour_dirs:
        if len(hh) != 2 or not hh.isdigit():
            continue
        hour_start = day_start + timedelta(hours=int(hh))
        if now <= hour_start + timedelta(hours=1) + SEAL_GRACE:
            continue  # hour still open

        hour_file = f"{date_path}/{hh}.parquet"
        chunk_dir = f"{date_path}/{hh}"
        rollup_file = f"{rollup_org}/{date_dir}/{hh}.parquet"

        if storage.exists(hour_file):
            # Already sealed — never rebuild. Drop any late/leftover chunks.
            # (Rollup recovery for this hour ran above.)
            _delete_subtree(storage, chunk_dir)
            continue

        try:
            _, chunk_files = storage.listdir(chunk_dir)
        except (NotImplementedError, OSError):
            continue
        chunks = sorted(f for f in chunk_files if f.endswith(".parquet"))
        if not chunks:
            _delete_subtree(storage, chunk_dir)
            continue

        if not _duckdb_copy(storage, [f"{chunk_dir}/{c}" for c in chunks], hour_file):
            continue  # slot saturated — retry next run, chunks intact

        try:
            _write_hour_rollup(storage, hour_file, rollup_file, hour_start)
        except Exception:
            # Rollup will be recomputed on the next compaction tick by the
            # recovery loop above.
            logger.error("Failed hour rollup %s/%s", date_dir, hh, exc_info=True)
        _delete_subtree(storage, chunk_dir)
        compacted += len(chunks)

    # Lazy daily roll once the whole day is sealed.
    if now > day_start + timedelta(days=1) + SEAL_GRACE:
        _roll_sealed_day(storage, raw_org, date_dir)
        _roll_sealed_day(storage, rollup_org, date_dir)
    return compacted


def _roll_sealed_day(storage, base_org: str, date_dir: str) -> None:
    """Concat a fully-sealed day's hour files into ``{date}.parquet``.

    Idempotent: if the day file already exists, just clears the leftover
    ``{date}/`` subtree. Used for both the raw and the rollup trees.
    """
    day_file = f"{base_org}/{date_dir}.parquet"
    date_path = f"{base_org}/{date_dir}"
    if storage.exists(day_file):
        _delete_subtree(storage, date_path)
        return
    try:
        _, hour_files = storage.listdir(date_path)
    except (NotImplementedError, OSError):
        return
    hours = sorted(f"{date_path}/{f}" for f in hour_files if f.endswith(".parquet"))
    if not hours:
        _delete_subtree(storage, date_path)
        return
    if _duckdb_copy(storage, hours, day_file):
        _delete_subtree(storage, date_path)
