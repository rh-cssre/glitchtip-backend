"""
Shared cold storage infrastructure for archiving partitions to Parquet via standalone DuckDB.

Auto-enables when a storage backend is configured (GLITCHTIP_COLD_STORAGE_BUCKET,
AWS_STORAGE_BUCKET_NAME, GLITCHTIP_COLD_STORAGE_DIR, or a "cold" STORAGES alias).
Old partitions are archived to Parquet files and queryable via DuckDB's in-process engine.

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
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.storage import storages
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

# Prefix for all cold storage files to prevent collisions with other data
COLD_STORAGE_PREFIX = "cold_storage"


def get_cold_storage_backend():
    """
    Get the django-storages backend for cold storage.

    Priority:
    1. "cold" alias in STORAGES setting (most flexible)
    2. S3 storage via GLITCHTIP_COLD_STORAGE_BUCKET or AWS_STORAGE_BUCKET_NAME
    3. Local filesystem via GLITCHTIP_COLD_STORAGE_DIR

    Returns None if no suitable storage backend is available.
    """
    # 1. Dedicated cold storage alias in STORAGES
    if "cold" in storages.backends:
        return storages["cold"]

    # 2. S3 bucket configured
    bucket = settings.GLITCHTIP_COLD_STORAGE_BUCKET
    if not bucket:
        bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
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


def is_duckdb_available() -> bool:
    """
    Check if DuckDB cold storage is enabled.

    Auto-enables when a storage backend is configured (bucket, directory,
    or "cold" STORAGES alias). Override with GLITCHTIP_ENABLE_DUCKDB=false
    to disable even when storage exists.
    """
    override = settings.GLITCHTIP_ENABLE_DUCKDB
    if override is not None:
        return str(override).lower() == "true"

    # Auto-detect: enable if a "cold" STORAGES alias is configured
    if "cold" in storages.backends:
        return True

    # Auto-detect: enable if a storage bucket is available
    bucket = settings.GLITCHTIP_COLD_STORAGE_BUCKET
    if not bucket:
        bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if bucket:
        return True

    # Auto-detect: enable if a local directory is configured
    if settings.GLITCHTIP_COLD_STORAGE_DIR:
        return True

    return False


def get_duckdb_connection(storage=None):
    """
    Create a standalone DuckDB connection, optionally configured for S3 access.

    For S3 backends: loads httpfs and configures credentials from the storage instance.
    For filesystem backends: returns a plain DuckDB connection (no extensions needed).

    Each call creates a fresh connection — no session state leaks.
    """
    import duckdb

    if storage is None:
        storage = get_cold_storage_backend()

    conn = duckdb.connect()

    if storage and _is_s3_storage(storage):
        # Load httpfs for S3 access
        conn.install_extension("httpfs")
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


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
) -> list[tuple[int, str]]:
    """
    Archive a partition to cold storage as per-org Parquet files.

    Each organization's data is exported to a separate file:
    cold_storage/{table}/org_{id}/{date}.parquet

    Reads from PostgreSQL via Django's connection, writes Parquet via
    standalone DuckDB.

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

    try:
        with connection.cursor() as cursor:
            # Find all orgs with data in this partition
            cursor.execute(
                f"SELECT DISTINCT organization_id FROM {partition_name} ORDER BY organization_id;"
            )
            org_ids = [row[0] for row in cursor.fetchall()]

            if not org_ids:
                logger.info(f"No data in {partition_name}, skipping")
                return []

            logger.info(
                f"Archiving {partition_name} for {len(org_ids)} orgs: {org_ids}"
            )

            # Export each org's data to a separate Parquet file
            for org_id in org_ids:
                relative_path = get_org_cold_storage_path(table_name, org_id, date_str)
                parquet_path = get_duckdb_parquet_path(storage, relative_path)

                # Read org's data from PostgreSQL
                cursor.execute(
                    select_sql.format(partition_name=partition_name),
                    [org_id],
                )
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()

                if not rows:
                    continue

                # Write to storage via standalone DuckDB
                import json

                # Normalize values for DuckDB (UUIDs to strings, dicts to JSON, lists to JSON)
                clean_rows = []
                for row in rows:
                    clean_row = []
                    for val in row:
                        if isinstance(val, dict):
                            clean_row.append(json.dumps(val))
                        elif isinstance(val, list):
                            clean_row.append(json.dumps(val))
                        elif hasattr(val, "hex"):  # UUID
                            clean_row.append(str(val))
                        else:
                            clean_row.append(val)
                    clean_rows.append(clean_row)

                # Ensure parent directory exists for filesystem backends
                if not _is_s3_storage(storage):
                    os.makedirs(os.path.dirname(parquet_path), exist_ok=True)

                duck_conn = get_duckdb_connection(storage)
                try:
                    col_defs = ", ".join(f"{c} {column_types[c]}" for c in columns)
                    duck_conn.execute(f"CREATE TABLE export_data({col_defs})")
                    duck_conn.executemany(
                        f"INSERT INTO export_data VALUES ({', '.join(['?'] * len(columns))})",
                        clean_rows,
                    )
                    duck_conn.execute(
                        f"COPY export_data TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD);"
                    )
                    archived_files.append((org_id, parquet_path))
                    logger.debug(f"Archived org {org_id} to {parquet_path}")
                finally:
                    duck_conn.close()

        logger.info(
            f"Archived {partition_name}: {len(archived_files)} org files created"
        )
        return archived_files

    except Exception as e:
        logger.error(f"Failed to archive {partition_name}: {e}")
        raise


def detach_partition(partition_name: str, parent_table: str) -> None:
    """
    Detach a partition from its parent table.

    This is done before dropping the partition after archival.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {parent_table} DETACH PARTITION {partition_name};")
    logger.info(f"Detached partition {partition_name} from {parent_table}")


def drop_partition(partition_name: str) -> None:
    """Drop a partition table after it has been archived."""
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {partition_name};")
    logger.info(f"Dropped partition {partition_name}")


def archive_and_swap_partition(
    partition_name: str,
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
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
        partition_name, date_str, table_name, column_types, select_sql
    )
    if not archived_files:
        logger.info(f"No data archived from {partition_name}")
        # Still proceed to drop empty partition

    # Step 2: Detach partition from parent table
    detach_partition(partition_name, table_name)

    # Step 3: Drop the original partition (and its hash sub-partitions via CASCADE)
    drop_partition(partition_name)

    logger.info(
        f"Successfully archived {partition_name}: {len(archived_files)} org files"
    )
    return True


def get_partitions_older_than(
    table_name: str, days: int, partition_suffix: str = ""
) -> list[tuple[str, datetime]]:
    """
    Get list of partitions older than the specified number of days.

    Args:
        table_name: Base table name (e.g., "logs_logevent")
        days: Number of days - partitions older than this are returned
        partition_suffix: Optional suffix to match

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

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {name_column} FROM {source_table}
            WHERE {name_column} LIKE %s
            AND {name_column} ~ %s
            ORDER BY {name_column};
            """,
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


def cleanup_cold_storage_for_org(
    org_id: int,
    retention_days: int,
    table_name: str,
) -> int:
    """
    Delete cold storage files older than retention period for an org.

    Computes paths directly from a 30-day window before the retention cutoff.
    No external state (cache/DB) needed - storage.delete() is a no-op on
    most backends if the file doesn't exist.

    Returns:
        Number of files deleted
    """
    storage = get_cold_storage_backend()
    if not storage:
        logger.warning("No storage backend available for cleanup")
        return 0

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count = 0

    # Sweep a 30-day window before the retention cutoff.
    # Files older than cutoff-30d would have been cleaned in prior runs.
    cleanup_window_days = 30
    for day_offset in range(cleanup_window_days):
        file_date = cutoff - timedelta(days=day_offset)
        date_str = file_date.strftime("%Y%m%d")
        storage_path = get_org_cold_storage_path(table_name, org_id, date_str)
        try:
            if storage.exists(storage_path):
                storage.delete(storage_path)
                deleted_count += 1
                logger.debug(f"Deleted {storage_path}")
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

    Only runs if GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED is True.
    For high-scale deployments, disable this and use S3 lifecycle policies.

    Returns:
        Total number of files deleted
    """
    if not settings.GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED:
        logger.info("Cold storage cleanup disabled, skipping (use lifecycle policies)")
        return 0

    if retention_days is None:
        retention_days = settings.GLITCHTIP_COLD_STORAGE_RETENTION_DAYS

    # Import here to avoid circular imports
    from apps.organizations_ext.models import Organization

    total_deleted = 0

    for org in Organization.objects.all().iterator():
        deleted = cleanup_cold_storage_for_org(org.id, retention_days, table_name)
        total_deleted += deleted

    logger.info(f"Cold storage cleanup complete: {total_deleted} files deleted")
    return total_deleted


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
        retention_days = settings.GLITCHTIP_COLD_STORAGE_RETENTION_DAYS

    # Archive hot -> cold
    archived = 0
    failed = 0
    partitions = get_partitions_older_than(table_name, hot_days)

    if partitions:
        logger.info(
            f"Archiving {len(partitions)} {table_name} partitions to cold storage"
        )
        for name, date in partitions:
            try:
                if archive_and_swap_partition(
                    name, table_name, column_types, select_sql
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

    # Try listing files under the org prefix
    try:
        _dirs, files = storage.listdir(org_prefix)
        for filename in files:
            file_path = f"{org_prefix}/{filename}"
            try:
                storage.delete(file_path)
                deleted_count += 1
            except Exception:
                logger.warning("Failed to delete cold file %s", file_path)
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
    column_types: dict[str, str] | None = None,
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
        column_types: DuckDB column types for the table

    Returns:
        Number of files rewritten or deleted
    """
    if not is_duckdb_available():
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    org_prefix = f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}"
    files_to_process = []

    # Collect file list
    try:
        _dirs, files = storage.listdir(org_prefix)
        files_to_process = [f for f in files if f.endswith(".parquet")]
    except (NotImplementedError, OSError):
        # Fall back to date sweep
        now = timezone.now()
        for day_offset in range(365):
            file_date = now - timedelta(days=day_offset)
            date_str = file_date.strftime("%Y%m%d")
            storage_path = get_org_cold_storage_path(table_name, org_id, date_str)
            try:
                if storage.exists(storage_path):
                    files_to_process.append(f"{date_str}.parquet")
            except Exception:
                pass

    if not files_to_process:
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

    for filename in files_to_process:
        date_str = filename.replace(".parquet", "")
        relative_path = get_org_cold_storage_path(table_name, org_id, date_str)
        parquet_path = get_duckdb_parquet_path(storage, relative_path)

        try:
            duck_conn = get_duckdb_connection(storage)
            try:
                # Count remaining rows after filtering
                count_sql = f"SELECT COUNT(*) FROM read_parquet('{parquet_path}') {where_clause}"
                remaining = duck_conn.execute(count_sql).fetchone()[0]

                if remaining == 0:
                    # No rows left — delete the file
                    duck_conn.close()
                    storage.delete(relative_path)
                    rewritten_count += 1
                    logger.debug("Deleted empty cold file %s", relative_path)
                    continue

                # Check if any rows were actually filtered out
                total_sql = f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
                total = duck_conn.execute(total_sql).fetchone()[0]

                if remaining == total:
                    # No data from this project in this file, skip
                    continue

                # Rewrite the file excluding the project's data
                rewrite_sql = f"""
                    COPY (
                        SELECT * FROM read_parquet('{parquet_path}')
                        {where_clause}
                    ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
                """
                duck_conn.execute(rewrite_sql)
                rewritten_count += 1
                logger.debug("Rewrote cold file %s", relative_path)
            finally:
                duck_conn.close()
        except Exception as e:
            error_str = str(e)
            if "No files found" in error_str or "Could not open" in error_str:
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
