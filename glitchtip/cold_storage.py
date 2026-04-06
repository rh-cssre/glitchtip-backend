"""
Shared cold storage infrastructure for archiving partitions to Parquet.

Requires explicit opt-in via GLITCHTIP_ENABLE_DUCKDB=true.
Old partitions are archived to Parquet files and queryable via DuckDB's in-process engine.

Write path: arro3 (Rust Arrow/Parquet via PyO3) — streams CSV→Parquet with
bounded memory and no temp files.
Read path: standalone DuckDB — analytical queries over Parquet files.

Uses standalone DuckDB (not pg_duckdb extension) so cold storage works with
any PostgreSQL provider including RDS, Aurora, Cloud SQL, etc. No Postgres
extensions required — DuckDB runs in the Python process, completely
independent of database connection pooling.

Architecture (per-org files):
1. Export each org's data from a partition to separate Parquet files
2. Path structure: cold_storage/{table}/org_{id}/{date}.parquet
3. Query cold storage by computing paths from (org_id, date_range)
4. No cross-org data in same file - enables future sharding

File deletion uses django-storages for backend abstraction (S3, GCS, Azure, filesystem).
High-scale deployments can disable manual cleanup and use S3 lifecycle policies.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.storage import storages
from django.db import connection, connections
from django.test.signals import setting_changed
from django.utils import timezone
from psycopg.sql import SQL, Identifier

logger = logging.getLogger(__name__)

# Prefix for all cold storage files to prevent collisions with other data
COLD_STORAGE_PREFIX = "cold_storage"

# Max rows per CSV chunk during archival — secondary safety limit.
# The primary limit is byte-based, auto-derived from DUCKDB_MEMORY_LIMIT.
ARCHIVE_CHUNK_ROWS = 50_000

# SOH (Start of Heading) control character used as CSV quote/escape character.
# Avoids ambiguity between JSON backslash-escaped quotes (\") and standard CSV
# double-quote escaping (""), which causes CSV parse errors on fields
# containing serialized JSON (e.g. issue event data::text).
CSV_QUOTE_CHAR = "\x01"


def _parse_duckdb_memory_bytes(limit_str: str) -> int | None:
    """Parse a DuckDB memory limit string (e.g. '128MB') to bytes.

    Returns None if the string is empty or unparseable.
    """
    if not limit_str:
        return None
    limit_str = limit_str.strip().upper()
    units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix, mult in units.items():
        if limit_str.endswith(suffix):
            return int(float(limit_str[: -len(suffix)]) * mult)
    try:
        return int(limit_str)
    except ValueError:
        return None


def _get_archive_chunk_bytes() -> int:
    """Derive the max CSV chunk size for archival batches.

    Bounds the Python-side bytearray buffer to prevent runaway memory growth
    when streaming large orgs. Uses DUCKDB_MEMORY_LIMIT as a proxy for the
    deployment's memory budget (since DuckDB is still used for reads).

    The 1 MB floor prevents degenerate cases with very low memory limits.
    """
    limit_str = getattr(settings, "DUCKDB_MEMORY_LIMIT", "") or ""
    mem_bytes = _parse_duckdb_memory_bytes(limit_str)
    if mem_bytes is None:
        # Memory limit disabled — DuckDB is unbounded, but we still cap the
        # Python-side CSV buffer to avoid runaway bytearray growth.
        return 64 * 1024 * 1024  # 64 MB
    return max(mem_bytes // 4, 1024 * 1024)


def get_cold_storage_backend():
    """
    Get the django-storages backend for cold storage.

    Priority:
    1. "cold" alias in STORAGES setting (most flexible)
    2. S3 storage via GLITCHTIP_COLD_STORAGE_BUCKET
    3. Local filesystem via GLITCHTIP_COLD_STORAGE_DIR

    Returns None if no suitable storage backend is available.
    """
    # 1. Dedicated cold storage alias in STORAGES
    if "cold" in storages.backends:
        return storages["cold"]

    # 2. S3 bucket configured
    bucket = settings.GLITCHTIP_COLD_STORAGE_BUCKET
    if bucket:
        try:
            from storages.backends.s3 import S3Storage

            return S3Storage(bucket_name=bucket)
        except ImportError:
            pass

    # 3. Local directory configured
    if settings.GLITCHTIP_COLD_STORAGE_DIR:
        from django.core.files.storage import FileSystemStorage

        return FileSystemStorage(location=settings.GLITCHTIP_COLD_STORAGE_DIR)

    return None


def _is_s3_storage(storage) -> bool:
    """Check if a storage backend is S3-based."""
    try:
        from storages.backends.s3 import S3Storage

        return isinstance(storage, S3Storage)
    except ImportError:
        return False


_duckdb_available: bool | None = None


def is_duckdb_available() -> bool:
    """
    Check if cold storage is enabled.

    Requires explicit opt-in via GLITCHTIP_ENABLE_DUCKDB=true.
    When explicitly enabled, trusts the deployment and skips backend
    inspection — the backend is resolved lazily on first use.

    Result is cached at module level since the setting doesn't change at
    runtime. The cache is automatically cleared by Django's setting_changed
    signal (fired by @override_settings in tests).
    """
    global _duckdb_available
    if _duckdb_available is not None:
        return _duckdb_available

    override = settings.GLITCHTIP_ENABLE_DUCKDB
    _duckdb_available = override is not None and str(override).lower() == "true"
    return _duckdb_available


def _reset_duckdb_available_cache(**kwargs):
    global _duckdb_available
    _duckdb_available = None


setting_changed.connect(_reset_duckdb_available_cache)


def get_duckdb_connection(storage=None):
    """
    Create a fresh standalone DuckDB connection, optionally configured for S3.

    Use this for write operations or when temp tables are needed (e.g. promotion).
    For read-only queries, prefer ``get_duckdb_read_connection()`` which caches
    a connection per thread.

    The caller is responsible for closing the returned connection.
    """
    conn = _create_duckdb_connection(storage)
    # Reduce DuckDB's internal buffer overhead for writes — archival is
    # background work that doesn't need parallelism or insertion-order
    # preservation.  Read connections keep defaults for query parallelism.
    conn.execute("SET threads = 1")
    conn.execute("SET preserve_insertion_order = false")
    return conn


# Thread-local storage for cached read-only DuckDB connections.
_thread_local = threading.local()


def get_duckdb_read_connection(storage=None):
    """
    Get a thread-local cached DuckDB connection for read-only queries.

    Avoids the overhead of creating a new DuckDB connection (and loading
    S3 extensions) on every query. The connection is reused across calls
    within the same thread and lazily created on first use.

    Do NOT call .close() on the returned connection — it is managed by
    the thread-local cache. Use ``close_duckdb_read_connection()`` for
    explicit cleanup (e.g. in tests).
    """
    conn = getattr(_thread_local, "duckdb_conn", None)
    if conn is not None:
        return conn
    conn = _create_duckdb_connection(storage)
    _thread_local.duckdb_conn = conn
    return conn


def close_duckdb_read_connection():
    """Close and discard the thread-local cached DuckDB connection."""
    conn = getattr(_thread_local, "duckdb_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.duckdb_conn = None


def _create_duckdb_connection(storage=None):
    """
    Create a standalone DuckDB connection, optionally configured for S3 access.

    For S3 backends: loads httpfs and configures credentials from the storage instance.
    For filesystem backends: returns a plain DuckDB connection (no extensions needed).

    Extensions must be pre-installed (Docker image or CI script).
    When DUCKDB_EXTENSION_DIRECTORY is set, autoinstall is disabled.
    """
    import duckdb

    if storage is None:
        storage = get_cold_storage_backend()

    config = {}
    ext_dir = getattr(settings, "DUCKDB_EXTENSION_DIRECTORY", None)
    if ext_dir:
        config["extension_directory"] = ext_dir
        config["autoinstall_known_extensions"] = "false"
    conn = duckdb.connect(config=config)

    memory_limit = getattr(settings, "DUCKDB_MEMORY_LIMIT", "128MB")

    # Always set memory limit to prevent OOM-killing the worker process.
    # Without a temp directory DuckDB can't spill to disk, so queries
    # exceeding the limit will fail — but that's better than an OOM kill
    # that takes down the entire worker.
    if memory_limit:
        conn.execute(f"SET memory_limit = '{memory_limit}'")

    # Limit DuckDB's thread pool. By default DuckDB spawns one thread per
    # HOST CPU core, which in Kubernetes means it sees the node's cores
    # (e.g. 64) rather than the pod's CPU limit (e.g. 2). Each thread
    # allocates scan buffers outside the memory_limit setting (which only
    # bounds DuckDB's internal buffer pool, not thread stacks or mmap'd
    # file regions), causing VmPeak to explode to 8+ GB.
    threads = getattr(settings, "DUCKDB_THREADS", 2)
    conn.execute(f"SET threads = {int(threads)}")

    # Auto-detect a writable temp directory for DuckDB spill-to-disk.
    # Explicit setting takes priority, then Python's tempfile default.
    temp_dir = getattr(settings, "DUCKDB_TEMP_DIRECTORY", "") or tempfile.gettempdir()
    if os.path.isdir(temp_dir) and os.access(temp_dir, os.W_OK):
        conn.execute(f"SET temp_directory = '{temp_dir}'")
    else:
        logger.warning(
            "No writable temp directory found (tried %s), DuckDB cannot spill to disk",
            temp_dir,
        )

    if storage and _is_s3_storage(storage):
        conn.load_extension("httpfs")

        # Configure S3 credentials from the storage backend
        access_key = getattr(storage, "access_key", None)
        secret_key = getattr(storage, "secret_key", None)
        endpoint_url = getattr(storage, "endpoint_url", None)

        if access_key:
            val = access_key.replace("'", "''")
            conn.execute(f"SET s3_access_key_id = '{val}';")
        if secret_key:
            val = secret_key.replace("'", "''")
            conn.execute(f"SET s3_secret_access_key = '{val}';")

        if endpoint_url:
            # Strip protocol prefix for DuckDB
            endpoint = endpoint_url.replace("http://", "").replace("https://", "")
            use_ssl = "true" if endpoint_url.startswith("https") else "false"
            conn.execute(f"SET s3_endpoint = '{endpoint}';")
            conn.execute(f"SET s3_use_ssl = {use_ssl};")
            conn.execute("SET s3_url_style = 'path';")

    return conn


def duckdb_quote_path(path: str) -> str:
    """Escape a file path for safe interpolation into DuckDB SQL string literals."""
    return path.replace("'", "''")


def get_duckdb_parquet_path(storage, relative_path: str) -> str:
    """
    Get the full path for DuckDB to read/write a Parquet file.

    For S3 backends: returns s3://bucket/relative_path
    For filesystem backends: returns the absolute local path
    """
    if _is_s3_storage(storage):
        return f"s3://{storage.bucket_name}/{relative_path}"
    # FileSystemStorage or similar — use storage.path() for absolute path
    return storage.path(relative_path)


def get_org_cold_storage_path(table_name: str, org_id: int, date_str: str) -> str:
    """Get the storage-relative path for an org's daily Parquet file (without bucket)."""
    return f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}.parquet"


def _get_chunk_dir(table_name: str, org_id: int, date_str: str) -> str:
    """Get the storage-relative directory for chunked Parquet files."""
    return f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}"


def _get_chunk_path(table_name: str, org_id: int, date_str: str, chunk: int) -> str:
    """Get the storage-relative path for a specific chunk file."""
    return f"{_get_chunk_dir(table_name, org_id, date_str)}/chunk_{chunk:03d}.parquet"


def _date_in_range(date_str: str, start_dt: datetime, end_dt: datetime) -> bool:
    """Check if a YYYYMMDD date string's day overlaps [start_dt, end_dt)."""
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d").replace(
            tzinfo=start_dt.tzinfo
        )
    except ValueError:
        return True  # Unknown format — include to be safe
    return file_date < end_dt and file_date + timedelta(days=1) > start_dt


def enumerate_org_parquet_files(
    storage,
    table_name: str,
    org_id: int,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> list[str]:
    """
    Enumerate Parquet files for an org, optionally filtered by date range.

    Handles both layouts:
    - Flat files: org_{id}/{date}.parquet
    - Chunk files: org_{id}/{date}/chunk_*.parquet

    When a flat file exists for a date, chunk files for that date are
    skipped (crash-safety: flat file indicates successful compaction).

    Returns list of storage-relative paths.
    """
    org_prefix = f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}"

    try:
        subdirs, flat_files = storage.listdir(org_prefix)
    except (NotImplementedError, OSError):
        return []

    paths = []
    compacted_dates: set[str] = set()

    # Flat files: org_{id}/{date}.parquet
    for f in sorted(flat_files):
        if not f.endswith(".parquet"):
            continue
        date_str = f.removesuffix(".parquet")
        if start_dt is not None and end_dt is not None:
            if not _date_in_range(date_str, start_dt, end_dt):
                continue
        paths.append(f"{org_prefix}/{f}")
        compacted_dates.add(date_str)

    # Chunk files in date subdirectories — skip dates with a flat file
    for subdir in sorted(subdirs):
        if subdir in compacted_dates:
            continue
        if start_dt is not None and end_dt is not None:
            if not _date_in_range(subdir, start_dt, end_dt):
                continue
        subdir_path = f"{org_prefix}/{subdir}"
        try:
            _, chunk_files = storage.listdir(subdir_path)
            for cf in sorted(chunk_files):
                if cf.endswith(".parquet"):
                    paths.append(f"{subdir_path}/{cf}")
        except (OSError, NotImplementedError):
            pass

    return paths


def get_parquet_paths_for_date(
    storage, table_name: str, org_id: int, date_str: str
) -> list[str]:
    """
    Return all Parquet file paths for an org+date — flat file and/or chunks.

    Used by single-event lookups to find the right file(s) to search.
    """
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d")
        file_date = timezone.make_aware(file_date)
    except ValueError:
        return []
    return enumerate_org_parquet_files(
        storage,
        table_name,
        org_id,
        start_dt=file_date,
        end_dt=file_date + timedelta(days=1),
    )


def _duckdb_type_to_arrow(column_types: dict[str, str]):
    """Convert DuckDB type names to arro3 DataType objects.

    Lazily imports arro3 to avoid module-level import cost on
    code paths that only read (not write) Parquet files.
    """
    import arro3.core as ac

    _map = {
        "VARCHAR": ac.DataType.utf8,
        "INTEGER": ac.DataType.int32,
        "BIGINT": ac.DataType.int64,
        "SMALLINT": ac.DataType.int16,
        "DOUBLE": ac.DataType.float64,
        "TIMESTAMP": ac.DataType.timestamp,
        "TIMESTAMP WITH TIME ZONE": ac.DataType.timestamp,
    }

    fields = []
    for col, dtype in column_types.items():
        arrow_type = _map.get(dtype)
        if arrow_type is None:
            raise ValueError(f"Unsupported DuckDB type for arro3 mapping: {dtype}")
        if dtype in ("TIMESTAMP", "TIMESTAMP WITH TIME ZONE"):
            fields.append(ac.Field(col, arrow_type("us")))
        else:
            fields.append(ac.Field(col, arrow_type()))
    return ac.Schema(fields)


def _parquet_encoding_opts(
    column_types: dict[str, str],
    dictionary_columns: set[str] | None = None,
) -> dict:
    """Build arro3 write_parquet encoding kwargs for optimal file size.

    High-cardinality string columns (IDs, body, JSON data) use
    DELTA_BYTE_ARRAY encoding — stores only byte-level differences between
    consecutive values, then ZSTD compresses the deltas. This typically
    produces files ~35% smaller than DuckDB's default.

    Low-cardinality columns (service, environment, host, numeric types)
    use dictionary encoding — stores each unique value once with integer
    indices.  ``dictionary_columns`` explicitly opts VARCHAR columns into
    dictionary encoding; numeric types always use it.
    """
    if dictionary_columns is None:
        dictionary_columns = set()

    col_dict_enabled: dict[str, bool] = {}
    col_encoding: dict[str, str] = {}

    for col, dtype in column_types.items():
        if dtype == "VARCHAR":
            if col in dictionary_columns:
                col_dict_enabled[col] = True
            else:
                col_dict_enabled[col] = False
                col_encoding[col] = "DELTA_BYTE_ARRAY"
        else:
            # Numeric types — dictionary works well (few unique values)
            col_dict_enabled[col] = True

    return {
        "column_dictionary_enabled": col_dict_enabled,
        "column_encoding": col_encoding,
    }


def _flush_csv_to_parquet(
    storage,
    csv_data: bytes | bytearray,
    column_types: dict[str, str],
    table_name: str,
    org_id: int,
    date_str: str,
    flat_path: str,
    chunk_num: int,
    total_rows: int,
    row_count: int,
    *,
    is_final_flush: bool = False,
    dictionary_columns: set[str] | None = None,
) -> tuple[int, int]:
    """
    Write CSV data to a Parquet file via arro3 (Rust Arrow/Parquet).

    Streams CSV→Arrow→Parquet without temp files. For filesystem backends,
    writes directly to the target path. For S3, writes to an in-memory
    buffer then uploads via django-storages.

    Returns updated (chunk_num, total_rows).
    Small orgs (first and only chunk) get a flat file.
    """
    import io

    import arro3.io as aio

    schema = _duckdb_type_to_arrow(column_types)
    encoding_opts = _parquet_encoding_opts(column_types, dictionary_columns)

    # First-and-only chunk → flat file; otherwise chunk dir
    if chunk_num == 0 and is_final_flush:
        out_path = flat_path
    else:
        out_path = _get_chunk_path(table_name, org_id, date_str, chunk_num)
        chunk_num += 1

    # Match max_row_group_size to actual rows to avoid over-allocation.
    row_group_size = min(row_count, 100_000)

    reader = aio.read_csv(
        io.BytesIO(csv_data),
        schema,
        has_header=True,
        quote=CSV_QUOTE_CHAR,
        escape=CSV_QUOTE_CHAR,
    )

    write_kwargs = {
        "compression": "zstd(3)",
        "max_row_group_size": row_group_size,
        **encoding_opts,
    }

    if _is_s3_storage(storage):
        # Write to in-memory buffer, then upload via django-storages.
        # Delete first to prevent save() from appending random suffixes
        # to avoid collisions (e.g. "file_kJsULkE.parquet").
        buf = io.BytesIO()
        aio.write_parquet(reader, buf, **write_kwargs)
        buf.seek(0)
        from django.core.files.base import ContentFile

        try:
            storage.delete(out_path)
        except Exception:
            logger.debug("Could not delete %s before save (may not exist)", out_path)
        storage.save(out_path, ContentFile(buf.read()))
    else:
        parquet_path = storage.path(out_path)
        os.makedirs(os.path.dirname(parquet_path), exist_ok=True)
        aio.write_parquet(reader, parquet_path, **write_kwargs)

    total_rows += row_count
    return chunk_num, total_rows


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
    dictionary_columns: set[str] | None = None,
    db_alias: str | None = None,
) -> list[tuple[int, str]]:
    """
    Archive a partition to cold storage as per-org Parquet files.

    Each organization's data is exported to a separate file:
    cold_storage/{table}/org_{id}/{date}.parquet

    Reads from PostgreSQL via Django's connection, writes Parquet via arro3.

    Args:
        partition_name: Name of the partition to archive
        date_str: Date string for the partition (e.g., "20260115")
        table_name: Parent table name
        column_types: Dict mapping column names to DuckDB types
        select_sql: SQL template for selecting data, with {partition_name} and {org_id} placeholders

    Returns:
        List of (org_id, parquet_path) tuples for archived files
    """
    if not is_duckdb_available():
        logger.info("duckdb not available, skipping archival")
        return []

    storage = get_cold_storage_backend()
    if not storage:
        logger.warning("No storage backend available for archival")
        return []

    archived_files = []
    db_conn = connections[db_alias] if db_alias else connection

    try:
        with db_conn.cursor() as cursor:
            # Find all orgs with data in this partition.
            # Check existence first to avoid aborting the transaction
            # (a failed query in psycopg3 puts the connection in error state).
            cursor.execute(
                "SELECT 1 FROM pg_tables WHERE tablename = %s", [partition_name]
            )
            if not cursor.fetchone():
                logger.info(
                    "Partition %s already dropped, skipping archival",
                    partition_name,
                )
                return []

            cursor.execute(
                SQL(
                    "SELECT DISTINCT organization_id FROM {} ORDER BY organization_id;"
                ).format(Identifier(partition_name))
            )
            org_ids = [row[0] for row in cursor.fetchall()]

            if not org_ids:
                logger.info(f"No data in {partition_name}, skipping")
                return []

            # Filter to orgs eligible for cold storage (paid tier when billing enabled)
            if settings.BILLING_ENABLED:
                from apps.organizations_ext.models import Organization

                eligible_ids = set(
                    Organization.objects.filter(
                        id__in=org_ids,
                        stripe_primary_subscription__isnull=False,
                        stripe_primary_subscription__price__price__gt=0,
                    ).values_list("id", flat=True)
                )
                skipped = len(org_ids) - len(eligible_ids)
                if skipped:
                    logger.info("Skipping cold archival for %d free-tier orgs", skipped)
                org_ids = [oid for oid in org_ids if oid in eligible_ids]

            logger.info(f"Archiving {partition_name} for {len(org_ids)} orgs")

            # Export each org's data to separate Parquet files via arro3.
            # Large orgs are split into chunks of ARCHIVE_CHUNK_ROWS
            # to bound memory usage.
            for org_id in org_ids:
                flat_path = get_org_cold_storage_path(table_name, org_id, date_str)
                chunk_dir = _get_chunk_dir(table_name, org_id, date_str)

                # Skip orgs already archived (flat file is atomic/complete)
                if storage.exists(flat_path):
                    archived_files.append(
                        (org_id, get_duckdb_parquet_path(storage, flat_path))
                    )
                    logger.debug("Parquet already exists for org %d, skipping", org_id)
                    continue

                # Delete any partial chunks from a previous crashed run
                # so we re-archive cleanly from Postgres.
                try:
                    _, existing_chunks = storage.listdir(chunk_dir)
                    for f in existing_chunks:
                        try:
                            storage.delete(f"{chunk_dir}/{f}")
                        except Exception:
                            pass
                except (OSError, NotImplementedError):
                    pass

                # Stream CSV via COPY TO STDOUT — constant Python memory,
                # O(N) Postgres work, PgBouncer-safe (single statement).
                # org_id is always an int from the database, safe to inline.
                #
                # Use SOH (\x01) as quote character instead of double-quote
                # to avoid ambiguity between JSON backslash-escaped quotes
                # (\") and CSV double-quote escaping ("").
                query_sql = select_sql.format(partition_name=partition_name).replace(
                    "%s", str(int(org_id)), 1
                )
                copy_sql = (
                    f"COPY ({query_sql}) TO STDOUT "
                    f"WITH (FORMAT CSV, HEADER, QUOTE E'\\x01', FORCE_QUOTE *)"
                )

                chunk_num = 0
                total_rows = 0
                chunk_bytes_limit = _get_archive_chunk_bytes()
                header = None
                csv_buf = bytearray()
                buf_rows = 0

                raw_conn = cursor.connection
                with raw_conn.cursor() as copy_cur:
                    with copy_cur.copy(copy_sql) as copy_op:
                        for line in copy_op:
                            if header is None:
                                header = bytes(line)
                                csv_buf = bytearray(header)
                                continue

                            csv_buf.extend(line)
                            buf_rows += 1

                            if (
                                len(csv_buf) >= chunk_bytes_limit
                                or buf_rows >= ARCHIVE_CHUNK_ROWS
                            ):
                                chunk_num, total_rows = _flush_csv_to_parquet(
                                    storage,
                                    csv_buf,
                                    column_types,
                                    table_name,
                                    org_id,
                                    date_str,
                                    flat_path,
                                    chunk_num,
                                    total_rows,
                                    buf_rows,
                                    dictionary_columns=dictionary_columns,
                                )
                                csv_buf = bytearray(header)
                                buf_rows = 0

                # Flush remaining rows
                if buf_rows > 0:
                    chunk_num, total_rows = _flush_csv_to_parquet(
                        storage,
                        csv_buf,
                        column_types,
                        table_name,
                        org_id,
                        date_str,
                        flat_path,
                        chunk_num,
                        total_rows,
                        buf_rows,
                        is_final_flush=True,
                        dictionary_columns=dictionary_columns,
                    )
                del csv_buf

                if total_rows > 0:
                    archived_files.append((org_id, flat_path))
                    logger.debug(
                        "Archived org %d: %d rows in %d file(s)",
                        org_id,
                        total_rows,
                        max(chunk_num, 1),
                    )

        logger.info(
            f"Archived {partition_name}: {len(archived_files)} org files created"
        )
        return archived_files

    except Exception as e:
        logger.error(f"Failed to archive {partition_name}: {e}")
        raise


def detach_partition(
    partition_name: str,
    parent_table: str,
    db_alias: str | None = None,
    max_retries: int = 3,
) -> None:
    """
    Detach a partition from its parent table using CONCURRENTLY.

    CONCURRENTLY avoids the ACCESS EXCLUSIVE lock that blocks concurrent
    INSERTs. It requires autocommit mode (cannot run inside a transaction
    block).

    Retries on deadlock errors with exponential backoff. Falls back to
    non-concurrent DETACH if all concurrent attempts fail. Safe to call if
    the partition is already detached or does not exist.
    """
    import time

    db_conn = connections[db_alias] if db_alias else connection
    db_conn.ensure_connection()
    raw_conn = db_conn.connection

    old_autocommit = raw_conn.autocommit
    last_error = None
    try:
        raw_conn.autocommit = True
        for attempt in range(max_retries):
            try:
                with raw_conn.cursor() as cursor:
                    cursor.execute(
                        SQL("ALTER TABLE {} DETACH PARTITION {} CONCURRENTLY;").format(
                            Identifier(parent_table), Identifier(partition_name)
                        )
                    )
                logger.info(
                    "Detached partition %s from %s", partition_name, parent_table
                )
                return
            except Exception as e:
                err_msg = str(e).lower()
                if "does not exist" in err_msg or "is not a partition" in err_msg:
                    logger.info(
                        "Partition %s already detached or does not exist",
                        partition_name,
                    )
                    return
                if "deadlock" in err_msg and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Deadlock detaching %s (attempt %d/%d), retrying in %ds",
                        partition_name,
                        attempt + 1,
                        max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    last_error = e
                    continue
                last_error = e
                break

        # All CONCURRENTLY attempts failed — try non-concurrent as fallback.
        # Takes ACCESS EXCLUSIVE lock briefly but avoids deadlock.
        logger.warning(
            "DETACH CONCURRENTLY failed for %s, falling back to non-concurrent",
            partition_name,
            exc_info=last_error,
        )
        try:
            with raw_conn.cursor() as cursor:
                cursor.execute(
                    SQL("ALTER TABLE {} DETACH PARTITION {};").format(
                        Identifier(parent_table), Identifier(partition_name)
                    )
                )
            logger.info(
                "Detached partition %s (non-concurrent) from %s",
                partition_name,
                parent_table,
            )
        except Exception as e:
            err_msg = str(e).lower()
            if "does not exist" in err_msg or "is not a partition" in err_msg:
                logger.info(
                    "Partition %s already detached or does not exist",
                    partition_name,
                )
            else:
                raise
    finally:
        raw_conn.autocommit = old_autocommit


def drop_partition(partition_name: str, db_alias: str | None = None) -> None:
    """Drop a partition table after it has been archived."""
    db_conn = connections[db_alias] if db_alias else connection
    db_conn.ensure_connection()
    raw_conn = db_conn.connection

    old_autocommit = raw_conn.autocommit
    try:
        raw_conn.autocommit = True
        with raw_conn.cursor() as cursor:
            cursor.execute(
                SQL("DROP TABLE IF EXISTS {};").format(Identifier(partition_name))
            )
        logger.info(f"Dropped partition {partition_name}")
    finally:
        raw_conn.autocommit = old_autocommit


def archive_and_swap_partition(
    partition_name: str,
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
    db_alias: str | None = None,
    dictionary_columns: set[str] | None = None,
) -> bool:
    """
    Full archival workflow: Export per-org files -> Detach -> Drop partition.

    Args:
        partition_name: Name of the partition to archive
        table_name: Parent table name
        column_types: Dict mapping column names to DuckDB types
        select_sql: SQL template for selecting data

    Returns:
        True if archival succeeded, False if skipped (duckdb not available)
    """
    if not is_duckdb_available():
        logger.info("duckdb not available, skipping archival workflow")
        return False

    # Extract date from partition name (format: tablename_YYYYMMDD)
    parts = partition_name.split("_")
    date_str = None
    for part in parts:
        if len(part) == 8 and part.isdigit():
            date_str = part
            break

    if not date_str:
        logger.error(f"Could not extract date from partition name: {partition_name}")
        return False

    # Step 1: Export per-org files to cold storage
    archived_files = archive_partition_per_org(
        partition_name,
        date_str,
        table_name,
        column_types,
        select_sql,
        dictionary_columns=dictionary_columns,
        db_alias=db_alias,
    )
    if not archived_files:
        logger.info(f"No data archived from {partition_name}")
        # Still proceed to drop empty partition

    # Step 2: Detach partition from parent table
    detach_partition(partition_name, table_name, db_alias=db_alias)

    # Step 3: Drop the original partition (and its hash sub-partitions via CASCADE)
    drop_partition(partition_name, db_alias=db_alias)

    logger.info(
        f"Successfully archived {partition_name}: {len(archived_files)} org files"
    )
    return True


def get_partitions_older_than(
    table_name: str,
    days: int,
    partition_suffix: str = "",
    db_alias: str | None = None,
) -> list[tuple[str, datetime]]:
    """
    Get list of partitions older than the specified number of days.

    Args:
        table_name: Base table name (e.g., "logs_logevent")
        days: Number of days - partitions older than this are returned
        partition_suffix: Optional suffix to match
        db_alias: Database alias to use (default: Django's default connection)

    Returns list of (partition_name, partition_date) tuples.
    """
    cutoff_date = timezone.now() - timedelta(days=days)

    # Build regex pattern based on whether we're looking for views or tables
    if partition_suffix:
        pattern = f"^{table_name}_[0-9]{{8}}_h[0-9]+{partition_suffix}$"
        source_table = "pg_views"
        name_column = "viewname"
    else:
        pattern = f"^{table_name}_[0-9]{{8}}$"
        source_table = "pg_tables"
        name_column = "tablename"

    db_conn = connections[db_alias] if db_alias else connection
    with db_conn.cursor() as cursor:
        col = Identifier(name_column)
        cursor.execute(
            SQL("SELECT {} FROM {} WHERE {} LIKE %s AND {} ~ %s ORDER BY {};").format(
                col, Identifier(source_table), col, col, col
            ),
            [f"{table_name}_%", pattern],
        )
        partitions = []
        for (name,) in cursor.fetchall():
            parts = name.replace(partition_suffix, "").split("_")
            date_str = None
            for part in parts:
                if len(part) == 8 and part.isdigit():
                    date_str = part
                    break

            if date_str:
                try:
                    partition_date = datetime.strptime(date_str, "%Y%m%d")
                    partition_date = timezone.make_aware(partition_date)
                    if partition_date < cutoff_date:
                        partitions.append((name, partition_date))
                except ValueError:
                    continue

    return partitions


def _is_date_before_cutoff(date_str: str, cutoff: datetime) -> bool:
    """Check if a YYYYMMDD string represents a date before the cutoff."""
    if len(date_str) != 8 or not date_str.isdigit():
        return False
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d")
        file_date = timezone.make_aware(file_date)
        return file_date < cutoff
    except ValueError:
        return False


def cleanup_cold_storage_for_org(
    org_id: int,
    retention_days: int,
    table_name: str,
    storage=None,
) -> int:
    """
    Delete cold storage files older than retention period for an org.

    Handles both file layouts:
    - Compacted flat files: org_{id}/{date}.parquet
    - Chunk files: org_{id}/{date}/chunk_*.parquet (pre-compaction)

    Uses a single listdir on the org prefix instead of per-day I/O sweeps.

    Returns:
        Number of files deleted
    """
    if storage is None:
        storage = get_cold_storage_backend()
    if not storage:
        return 0

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count = 0
    org_prefix = f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}"

    try:
        subdirs, files = storage.listdir(org_prefix)
    except Exception:
        return 0

    # Delete expired flat files: org_{id}/{date}.parquet
    for filename in files:
        if not filename.endswith(".parquet"):
            continue
        date_str = filename.removesuffix(".parquet")
        if _is_date_before_cutoff(date_str, cutoff):
            try:
                storage.delete(f"{org_prefix}/{filename}")
                deleted_count += 1
            except Exception:
                pass

    # Delete expired chunk directories: org_{id}/{date}/chunk_*.parquet
    for subdir in subdirs:
        if not _is_date_before_cutoff(subdir, cutoff):
            continue
        chunk_dir = f"{org_prefix}/{subdir}"
        try:
            _, chunk_files = storage.listdir(chunk_dir)
            for f in chunk_files:
                try:
                    storage.delete(f"{chunk_dir}/{f}")
                    deleted_count += 1
                except Exception:
                    pass
            try:
                os.rmdir(storage.path(chunk_dir))
            except (OSError, NotImplementedError):
                pass
        except Exception:
            pass

    if deleted_count:
        logger.info(f"Deleted {deleted_count} cold files for org {org_id}")

    return deleted_count


def cleanup_all_cold_storage(
    retention_days: int | None = None,
    table_name: str = "logs_logevent",
) -> int:
    """
    Delete cold storage files older than retention period for all orgs.

    Discovers orgs from storage directory listing instead of querying the
    database. Only processes orgs that actually have cold data.

    Only runs if GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED is True.
    For high-scale deployments, disable this and use S3 lifecycle policies.

    Returns:
        Total number of files deleted
    """
    if not settings.GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED:
        logger.info("Cold storage cleanup disabled, skipping (use lifecycle policies)")
        return 0

    if retention_days is None:
        retention_days = settings.GLITCHTIP_RETENTION_DAYS

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    # Discover orgs from storage directory instead of querying the database.
    # Only orgs with actual cold data will have directories.
    table_prefix = f"{COLD_STORAGE_PREFIX}/{table_name}"
    try:
        org_dirs, _ = storage.listdir(table_prefix)
    except Exception:
        return 0

    total_deleted = 0
    for dirname in org_dirs:
        if not dirname.startswith("org_"):
            continue
        try:
            org_id = int(dirname.removeprefix("org_"))
        except ValueError:
            continue
        deleted = cleanup_cold_storage_for_org(
            org_id, retention_days, table_name, storage=storage
        )
        total_deleted += deleted

    if total_deleted:
        logger.info(f"Cold storage cleanup complete: {total_deleted} files deleted")
    return total_deleted


def query_cold_parquet_files(
    organization_id: int,
    table_name: str,
    select_columns: str,
    where_sql: str,
    params: list,
    limit_param: str,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    limit: int | None = None,
) -> list[tuple]:
    """
    Query cold storage parquet files individually with per-file error handling.

    Instead of a single glob query (which fails entirely if any file is corrupt),
    this enumerates files in the org directory and queries each one separately.
    Corrupt files are logged at ERROR level and skipped; valid results are merged.

    Files are iterated newest-first (matching ORDER BY id DESC) so that queries
    for recent data (e.g., latest event) can stop early without scanning all files.

    Args:
        organization_id: Org whose files to query
        table_name: PG table name (e.g., "logs_logevent")
        select_columns: Column list for SELECT clause
        where_sql: WHERE clause with DuckDB $N positional parameters
        params: Parameter values (without limit — limit_param references it)
        limit_param: DuckDB positional parameter for LIMIT (e.g., "$5")
        start_dt: Optional start datetime for date-based file filtering
        end_dt: Optional end datetime for date-based file filtering
        limit: Optional early-exit limit — stop scanning once this many rows collected

    Returns:
        List of raw row tuples from all successfully read files.
    """
    storage = get_cold_storage_backend()
    if not storage:
        return []

    parquet_paths = enumerate_org_parquet_files(
        storage, table_name, organization_id, start_dt, end_dt
    )
    if not parquet_paths:
        return []

    all_rows: list[tuple] = []
    duck_conn = get_duckdb_read_connection(storage)
    try:
        # Iterate newest files first — ids are UUIDv7 (time-ordered) and queries
        # use ORDER BY id DESC, so newer files have higher-ranked results.
        # Early exit once we have enough rows since older files cannot outrank them.
        for relative_path in reversed(parquet_paths):
            parquet_path = get_duckdb_parquet_path(storage, relative_path)
            sql = f"""
                SELECT {select_columns}
                FROM read_parquet('{duckdb_quote_path(parquet_path)}')
                WHERE {where_sql}
                ORDER BY id DESC
                LIMIT {limit_param};
            """
            try:
                result = duck_conn.execute(sql, params)
                all_rows.extend(result.fetchall())
                if limit is not None and len(all_rows) >= limit:
                    break
            except Exception:
                close_duckdb_read_connection()
                duck_conn = get_duckdb_read_connection(storage)
                logger.error(
                    "Corrupt parquet file skipped: %s",
                    relative_path,
                    exc_info=True,
                )
    except Exception:
        close_duckdb_read_connection()
        raise

    return all_rows


def is_missing_file_error(exc: Exception) -> bool:
    """Check if a DuckDB exception indicates a missing/inaccessible Parquet file."""
    error_str = str(exc)
    return any(
        msg in error_str
        for msg in ("No files found", "Could not open", "404", "Not Found")
    )


def parse_json_field(val) -> dict:
    """Parse a value that may be dict, JSON string, or None into a dict."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def parse_json_list_field(val) -> list:
    """Parse a value that may be list, JSON string, or None into a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def archive_and_cleanup_partitions(
    table_name: str,
    hot_days: int,
    column_types: dict[str, str],
    select_sql: str,
    retention_days: int | None = None,
    db_alias: str | None = None,
    dictionary_columns: set[str] | None = None,
) -> tuple[int, int, int]:
    """
    Archive old hot partitions to cold storage and clean up expired cold files.

    This is the standard maintenance pattern used by all partitioned tables
    with cold storage support. Each table provides its own column_types and
    select_sql; this function handles the archive loop and cold cleanup.

    Args:
        table_name: PG table name (e.g., "issue_events_issueevent")
        hot_days: Days to keep in hot storage before archiving
        column_types: DuckDB column type mapping for Parquet export
        select_sql: SQL template with {partition_name} placeholder
        retention_days: Days to keep cold files (default from settings)

    Returns:
        Tuple of (archived_count, failed_count, deleted_cold_count)
    """
    if not is_duckdb_available():
        return (0, 0, 0)

    if retention_days is None:
        retention_days = settings.GLITCHTIP_RETENTION_DAYS

    # Archive hot -> cold
    archived = 0
    failed = 0
    partitions = get_partitions_older_than(table_name, hot_days, db_alias=db_alias)

    if partitions:
        logger.info(
            f"Archiving {len(partitions)} {table_name} partitions to cold storage"
        )
        for name, date in partitions:
            try:
                if archive_and_swap_partition(
                    name,
                    table_name,
                    column_types,
                    select_sql,
                    db_alias=db_alias,
                    dictionary_columns=dictionary_columns,
                ):
                    archived += 1
                    logger.info(f"Archived partition {name}")
                else:
                    failed += 1
                    logger.warning(f"Failed to archive partition {name}")
            except Exception as e:
                failed += 1
                logger.error(f"Error archiving partition {name}: {e}")

        logger.info(
            f"{table_name} archival complete: {archived} archived, {failed} failed"
        )

    # Delete expired cold storage files
    deleted = cleanup_all_cold_storage(
        retention_days=retention_days, table_name=table_name
    )
    if deleted:
        logger.info(f"{table_name} cold cleanup: {deleted} files deleted")

    return (archived, failed, deleted)


def delete_org_cold_storage(
    org_id: int,
    table_name: str,
) -> int:
    """
    Delete all cold storage files for an organization.

    Used when an organization is being permanently deleted.
    Lists files under the org's prefix and deletes them all.

    Falls back to a date sweep (365 days) if listdir is unavailable.

    Returns:
        Number of files deleted
    """
    storage = get_cold_storage_backend()
    if not storage:
        logger.warning("No storage backend available for org cold storage deletion")
        return 0

    org_prefix = f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}"
    deleted_count = 0

    # Try listing files under the org prefix (including subdirectories
    # for chunk-file layouts like performance_spans/{date}/chunk_*.parquet)
    try:
        dirs, files = storage.listdir(org_prefix)
        for filename in files:
            file_path = f"{org_prefix}/{filename}"
            try:
                storage.delete(file_path)
                deleted_count += 1
            except Exception:
                logger.warning("Failed to delete cold file %s", file_path)
        # Recurse into subdirectories (e.g. date dirs with chunk files)
        for subdir in dirs:
            subdir_path = f"{org_prefix}/{subdir}"
            try:
                _, subfiles = storage.listdir(subdir_path)
                for subfile in subfiles:
                    try:
                        storage.delete(f"{subdir_path}/{subfile}")
                        deleted_count += 1
                    except Exception:
                        logger.warning(
                            "Failed to delete cold file %s/%s", subdir_path, subfile
                        )
                # Try to remove the empty directory (filesystem only)
                if not org_prefix.startswith("s3://"):
                    try:
                        import os

                        os.rmdir(storage.path(subdir_path))
                    except OSError:
                        pass
            except (NotImplementedError, OSError):
                pass
    except (NotImplementedError, OSError):
        # listdir not supported — fall back to date sweep
        now = timezone.now()
        for day_offset in range(365):
            file_date = now - timedelta(days=day_offset)
            date_str = file_date.strftime("%Y%m%d")
            storage_path = get_org_cold_storage_path(table_name, org_id, date_str)
            try:
                if storage.exists(storage_path):
                    storage.delete(storage_path)
                    deleted_count += 1
            except Exception:
                pass

    if deleted_count:
        logger.info(
            "Deleted %d cold files for org %d (%s)", deleted_count, org_id, table_name
        )

    return deleted_count


def rewrite_parquet_excluding_project(
    org_id: int,
    project_id: int | None = None,
    issue_ids: list[int] | None = None,
    table_name: str = "logs_logevent",
) -> int:
    """
    Rewrite Parquet files for an org, excluding a deleted project's data.

    For logs_logevent: filters by project_id != deleted project.
    For issue_events_issueevent: filters by issue_id NOT IN (deleted project's issues).

    Processes one file at a time to bound memory usage.

    Args:
        org_id: Organization ID
        project_id: Project ID to exclude (used for logs)
        issue_ids: Issue IDs to exclude (used for issue events)
        table_name: Table name for path construction

    Returns:
        Number of files rewritten or deleted
    """
    if not is_duckdb_available():
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    parquet_paths = enumerate_org_parquet_files(storage, table_name, org_id)
    if not parquet_paths:
        return 0

    # Build the WHERE filter
    if table_name == "issue_events_issueevent" and issue_ids:
        placeholders = ", ".join(str(int(iid)) for iid in issue_ids)
        where_clause = f"WHERE issue_id NOT IN ({placeholders})"
    elif project_id is not None:
        where_clause = f"WHERE project_id != {int(project_id)}"
    else:
        return 0

    rewritten_count = 0

    for relative_path in parquet_paths:
        parquet_path = get_duckdb_parquet_path(storage, relative_path)
        duck_conn = get_duckdb_read_connection(storage)
        try:
            quoted = duckdb_quote_path(parquet_path)

            # Count remaining rows after filtering
            remaining = duck_conn.execute(
                f"SELECT COUNT(*) FROM read_parquet('{quoted}') {where_clause}"
            ).fetchone()[0]

            if remaining == 0:
                # No rows left — delete the file
                storage.delete(relative_path)
                rewritten_count += 1
                logger.debug("Deleted empty cold file %s", relative_path)
                continue

            # Check if any rows were actually filtered out
            total = duck_conn.execute(
                f"SELECT COUNT(*) FROM read_parquet('{quoted}')"
            ).fetchone()[0]

            if remaining == total:
                # No data from this project in this file, skip
                continue

            # Rewrite to a temp file then replace for crash safety.
            # DuckDB handles both read+filter and write here — this is a
            # rare operation (project deletion only) so we accept DuckDB
            # for the write rather than adding a pyarrow dependency just
            # to bridge DuckDB→arro3.
            is_s3 = parquet_path.startswith("s3://")
            write_path = parquet_path if is_s3 else parquet_path + ".tmp"
            duck_conn.execute(f"""
                COPY (
                    SELECT * FROM read_parquet('{quoted}')
                    {where_clause}
                ) TO '{duckdb_quote_path(write_path)}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """)
            if not is_s3:
                os.rename(write_path, parquet_path)
            rewritten_count += 1
            logger.debug("Rewrote cold file %s", relative_path)
        except Exception as e:
            if is_missing_file_error(e):
                close_duckdb_read_connection()
                duck_conn = get_duckdb_read_connection(storage)
                continue
            logger.warning("Error rewriting cold file %s: %s", relative_path, e)

    if rewritten_count:
        logger.info(
            "Rewrote %d cold files for org %d excluding project %s (%s)",
            rewritten_count,
            org_id,
            project_id or f"issues={issue_ids}",
            table_name,
        )

    return rewritten_count
