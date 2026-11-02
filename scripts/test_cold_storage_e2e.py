#!/usr/bin/env python
"""
End-to-end cold storage tests for GlitchTip's DuckDB archival layer.

Tests the full lifecycle: insert → archive → query union → degradation.
Run inside the web container with cold storage enabled:

    docker compose exec \
        -e GLITCHTIP_COLD_STORAGE_DIR=/tmp/cold_storage_test \
        -e GLITCHTIP_EVENT_HOT_DAYS=0 \
        -e GLITCHTIP_LOG_HOT_DAYS=0 \
        web python scripts/test_cold_storage_e2e.py
"""

import gc
import json
import logging
import os
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID

# Django setup
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
import django

django.setup()

from django.db import connection

from apps.issue_events.cold_storage import (
    get_event_from_cold,
    query_cold_events,
)
from apps.logs.api import (
    query_cold_storage as query_cold_logs,
)
from apps.logs.api import (
    query_hot_storage as query_hot_logs,
)
from apps.logs.cold_storage import (
    EXPORT_COLUMN_TYPES as LOG_COLUMN_TYPES,
)
from apps.logs.cold_storage import (
    LOGS_SELECT_SQL,
)
from glitchtip.cold_storage import (
    archive_and_swap_partition,
    archive_partition_per_org,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    get_org_cold_storage_path,
    get_parquet_paths_for_date,
    get_partitions_older_than,
    is_duckdb_available,
)
from glitchtip.partition_manager import PartitionManager, UUID7Helper

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("cold_storage_e2e")

# ── Globals ─────────────────────────────────────────────────────────

COLD_DIR = "/tmp/cold_storage_test"
RESULTS: list[dict] = []
ORG_ID: int | None = None
PROJECT_ID: int | None = None


# ── Helpers ─────────────────────────────────────────────────────────


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"name": name, "status": status, "detail": detail})
    icon = "✓" if passed else "✗"
    logger.info(f"  {icon} {name}")
    if detail:
        for line in detail.strip().splitlines():
            logger.info(f"      {line}")


def setup_org_and_project():
    """Get or create a test org and project for cold storage tests."""
    global ORG_ID, PROJECT_ID
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project

    org = Organization.objects.first()
    if not org:
        org = Organization.objects.create(name="cold-test-org", slug="cold-test-org")
    ORG_ID = org.id

    project = Project.objects.filter(organization=org).first()
    if not project:
        project = Project.objects.create(name="cold-test-project", organization=org)
    PROJECT_ID = project.id
    logger.info(f"Using org_id={ORG_ID}, project_id={PROJECT_ID}")


def clean_cold_storage():
    """Remove all cold storage test files."""
    if os.path.exists(COLD_DIR):
        shutil.rmtree(COLD_DIR)
    os.makedirs(COLD_DIR, exist_ok=True)


def create_log_partition(date: datetime) -> str:
    """Create a log partition for the given date and return its name."""
    manager = PartitionManager()
    date_str = date.strftime("%Y%m%d")
    partition_name = f"logs_logevent_{date_str}"
    next_date = date + timedelta(days=1)

    sqls = manager.create_time_partition(
        parent_table="logs_logevent",
        partition_name=partition_name,
        start_date=date,
        end_date=next_date,
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
        partition_column="id",
    )
    with connection.cursor() as cursor:
        for sql in sqls:
            cursor.execute(sql)
    return partition_name


def drop_partition_if_exists(partition_name: str, parent_table: str = "logs_logevent"):
    """Safely drop a partition, ignoring errors if it doesn't exist."""
    with connection.cursor() as cursor:
        try:
            cursor.execute(f"ALTER TABLE {parent_table} DETACH PARTITION {partition_name}")
        except Exception:
            connection.connection.rollback()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {partition_name} CASCADE")
        except Exception:
            connection.connection.rollback()


def bulk_insert_logs(date: datetime, count: int, org_id: int = None, project_id: int = None) -> list[UUID]:
    """Batch-insert synthetic log events into Postgres. Returns list of IDs."""
    org = org_id or ORG_ID
    proj = project_id or PROJECT_ID
    ids = []

    with connection.cursor() as cursor:
        batch = []
        for i in range(count):
            offset_ms = int((i / max(count, 1)) * 86400 * 1000)
            event_time = date + timedelta(milliseconds=offset_ms)
            event_id = UUID7Helper.from_datetime(event_time)
            ids.append(event_id)
            batch.append(
                cursor.mogrify(
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        str(event_id), org, proj, 2, 9,
                        f"Test log {i} for {date.strftime('%Y-%m-%d')}",
                        "test-svc", json.dumps({"i": i}),
                        "test",
                    ],
                )
            )
            if len(batch) >= 5000:
                values = ",".join(batch)
                cursor.execute(
                    "INSERT INTO logs_logevent "
                    "(id, organization_id, project_id, level, severity_number, "
                    "body, service, data, environment) VALUES " + values
                    + " ON CONFLICT DO NOTHING"
                )
                batch = []
        if batch:
            values = ",".join(batch)
            cursor.execute(
                "INSERT INTO logs_logevent "
                "(id, organization_id, project_id, level, severity_number, "
                "body, service, data, environment) VALUES " + values
                + " ON CONFLICT DO NOTHING"
            )
    return ids


def count_parquet_rows(org_id: int, date_str: str, table_name: str = "logs_logevent") -> int:
    """Count rows across all parquet files (flat + chunks) for an org+date."""
    storage = get_cold_storage_backend()
    if not storage:
        return 0
    paths = get_parquet_paths_for_date(storage, table_name, org_id, date_str)
    if not paths:
        return 0
    total = 0
    try:
        duck = get_duckdb_connection(storage)
        try:
            for rel_path in paths:
                parquet_path = get_duckdb_parquet_path(storage, rel_path)
                total += duck.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
                ).fetchone()[0]
        finally:
            duck.close()
    except Exception:
        return 0
    return total


def parquet_exists(org_id: int, date_str: str, table_name: str = "logs_logevent") -> bool:
    storage = get_cold_storage_backend()
    if not storage:
        return False
    relative_path = get_org_cold_storage_path(table_name, org_id, date_str)
    return storage.exists(relative_path)


def get_parquet_path(org_id: int, date_str: str, table_name: str = "logs_logevent") -> str:
    storage = get_cold_storage_backend()
    relative_path = get_org_cold_storage_path(table_name, org_id, date_str)
    return get_duckdb_parquet_path(storage, relative_path)


def write_parquet_directly(date: datetime, count: int, org_id: int = None, project_id: int = None):
    """Write a parquet file directly (bypass PG) for speed. Used in memory tests."""
    org = org_id or ORG_ID
    proj = project_id or PROJECT_ID
    date_str = date.strftime("%Y%m%d")
    storage = get_cold_storage_backend()

    duck = get_duckdb_connection(storage)
    try:
        col_defs = ", ".join(f"{c} {LOG_COLUMN_TYPES[c]}" for c in LOG_COLUMN_TYPES)
        duck.execute(f"CREATE TABLE export_data({col_defs})")

        rows = []
        for i in range(count):
            offset_ms = int((i / max(count, 1)) * 86400 * 1000)
            event_time = date + timedelta(milliseconds=offset_ms)
            event_id = UUID7Helper.from_datetime(event_time)
            rows.append([
                str(event_id), None, org, proj, None,
                2, 9, f"Synth event {i}", "synth-svc", "test",
                "localhost", json.dumps({"day": date_str, "i": i}),
            ])

        duck.executemany(
            f"INSERT INTO export_data VALUES ({', '.join(['?'] * len(LOG_COLUMN_TYPES))})",
            rows,
        )

        relative_path = get_org_cold_storage_path("logs_logevent", org, date_str)
        parquet_path = get_duckdb_parquet_path(storage, relative_path)
        os.makedirs(os.path.dirname(parquet_path), exist_ok=True)
        duck.execute(f"COPY export_data TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD);")
    finally:
        duck.close()


# ── Test 1: Migration Idempotency ───────────────────────────────────


def test_migration_idempotency():
    logger.info("\n=== Test 1: Migration Idempotency ===")
    clean_cold_storage()

    target_date = datetime(2025, 6, 15, tzinfo=timezone.utc)
    date_str = "20250615"
    partition_name = create_log_partition(target_date)

    bulk_insert_logs(target_date, 100)

    # First archive (export only, don't detach)
    archived1 = archive_partition_per_org(
        partition_name=partition_name,
        date_str=date_str,
        table_name="logs_logevent",
        column_types=LOG_COLUMN_TYPES,
        select_sql=LOGS_SELECT_SQL,
    )
    count1 = count_parquet_rows(ORG_ID, date_str)
    record("1a: First migration produces correct count", count1 == 100, f"Expected 100, got {count1}")

    # Re-run archive (should overwrite, not duplicate)
    archived2 = archive_partition_per_org(
        partition_name=partition_name,
        date_str=date_str,
        table_name="logs_logevent",
        column_types=LOG_COLUMN_TYPES,
        select_sql=LOGS_SELECT_SQL,
    )
    count2 = count_parquet_rows(ORG_ID, date_str)
    record("1b: Re-run produces same count (no duplicates)", count2 == 100, f"Expected 100, got {count2}")

    # PG data intact (we didn't detach yet)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {partition_name} WHERE organization_id = %s", [ORG_ID])
        pg_count = cursor.fetchone()[0]
    record("1c: Postgres data intact after archive-only", pg_count == 100, f"PG count={pg_count}")

    # Full swap (detach + drop)
    ok = archive_and_swap_partition(partition_name, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)
    record("1d: archive_and_swap succeeds", ok, f"Returned {ok}")

    # Partition is gone
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM pg_tables WHERE tablename = %s", [partition_name])
        still_exists = cursor.fetchone()[0] > 0
    record("1e: Partition dropped after swap", not still_exists, f"still_exists={still_exists}")


# ── Test 2: Migration Edge Cases ────────────────────────────────────


def test_migration_edge_cases():
    logger.info("\n=== Test 2: Migration Edge Cases ===")
    clean_cold_storage()

    # 2a: Zero events
    empty_date = datetime(2025, 3, 1, tzinfo=timezone.utc)
    empty_part = create_log_partition(empty_date)
    archived = archive_partition_per_org(
        partition_name=empty_part, date_str="20250301",
        table_name="logs_logevent", column_types=LOG_COLUMN_TYPES, select_sql=LOGS_SELECT_SQL,
    )
    has_file = parquet_exists(ORG_ID, "20250301")
    record("2a: Zero events — no parquet file", len(archived) == 0 and not has_file,
           f"archived={len(archived)}, file_exists={has_file}")
    drop_partition_if_exists(empty_part)

    # 2b: Single event
    single_date = datetime(2025, 3, 2, tzinfo=timezone.utc)
    single_part = create_log_partition(single_date)
    bulk_insert_logs(single_date, 1)
    archived = archive_partition_per_org(
        partition_name=single_part, date_str="20250302",
        table_name="logs_logevent", column_types=LOG_COLUMN_TYPES, select_sql=LOGS_SELECT_SQL,
    )
    count = count_parquet_rows(ORG_ID, "20250302")
    record("2b: Single event migrated", count == 1, f"parquet_count={count}")
    drop_partition_if_exists(single_part)

    # 2c: Large batch (50k)
    large_date = datetime(2025, 3, 3, tzinfo=timezone.utc)
    large_part = create_log_partition(large_date)
    LARGE_COUNT = 50_000
    logger.info(f"  Inserting {LARGE_COUNT} events...")
    t0 = time.time()
    bulk_insert_logs(large_date, LARGE_COUNT)
    logger.info(f"  Inserted in {time.time() - t0:.1f}s")

    t0 = time.time()
    archived = archive_partition_per_org(
        partition_name=large_part, date_str="20250303",
        table_name="logs_logevent", column_types=LOG_COLUMN_TYPES, select_sql=LOGS_SELECT_SQL,
    )
    archive_time = time.time() - t0
    pq_count = count_parquet_rows(ORG_ID, "20250303")
    record(f"2c: {LARGE_COUNT} events migrated ({archive_time:.1f}s)",
           pq_count == LARGE_COUNT, f"parquet_count={pq_count}")
    drop_partition_if_exists(large_part)

    # 2d: No partitions to migrate
    partitions = get_partitions_older_than("logs_logevent", 99999)
    record("2d: No partitions to migrate — empty list", len(partitions) == 0,
           f"Got {len(partitions)}")


# ── Test 3: Query Union Correctness ─────────────────────────────────


def test_query_union_correctness():
    logger.info("\n=== Test 3: Query Union Correctness ===")
    clean_cold_storage()

    cold_date = datetime(2025, 4, 10, tzinfo=timezone.utc)
    hot_date = datetime(2025, 4, 11, tzinfo=timezone.utc)

    cold_part = create_log_partition(cold_date)
    hot_part = create_log_partition(hot_date)

    cold_ids = bulk_insert_logs(cold_date, 50)
    hot_ids = bulk_insert_logs(hot_date, 50)

    # Archive cold partition
    archive_and_swap_partition(cold_part, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)

    full_start = cold_date
    full_end = hot_date + timedelta(days=1)

    # 3a: Full range
    hot_results = query_hot_logs(organization_id=ORG_ID, start_dt=full_start, end_dt=full_end, limit=200)
    cold_results = query_cold_logs(organization_id=ORG_ID, start_dt=full_start, end_dt=full_end, limit=200)
    total = len(hot_results) + len(cold_results)
    record("3a: Full range union = correct count",
           total == 100, f"hot={len(hot_results)}, cold={len(cold_results)}, total={total}")

    # 3b: No duplicates
    all_ids = {r.id for r in hot_results} | {r.id for r in cold_results}
    record("3b: No duplicate events", len(all_ids) == total, f"unique={len(all_ids)}, total={total}")

    # 3c: Hot-only range
    hot_only = query_hot_logs(organization_id=ORG_ID, start_dt=hot_date, end_dt=hot_date + timedelta(days=1), limit=200)
    record("3c: Hot-only returns 50", len(hot_only) == 50, f"got {len(hot_only)}")

    # 3d: Cold-only range
    cold_only = query_cold_logs(organization_id=ORG_ID, start_dt=cold_date, end_dt=cold_date + timedelta(days=1), limit=200)
    record("3d: Cold-only returns 50", len(cold_only) == 50, f"got {len(cold_only)}")

    # 3e: Boundary — events from both tiers
    boundary_start = cold_date + timedelta(hours=23)
    boundary_end = hot_date + timedelta(hours=1)
    hot_b = query_hot_logs(organization_id=ORG_ID, start_dt=boundary_start, end_dt=boundary_end, limit=200)
    cold_b = query_cold_logs(organization_id=ORG_ID, start_dt=boundary_start, end_dt=boundary_end, limit=200)
    record("3e: Boundary has events from both tiers",
           len(hot_b) > 0 and len(cold_b) > 0,
           f"hot_boundary={len(hot_b)}, cold_boundary={len(cold_b)}")

    drop_partition_if_exists(hot_part)


# ── Test 4: Query Graceful Degradation ──────────────────────────────


def test_query_degradation():
    logger.info("\n=== Test 4: Query Graceful Degradation ===")
    clean_cold_storage()

    day1 = datetime(2025, 5, 1, tzinfo=timezone.utc)
    day2 = datetime(2025, 5, 2, tzinfo=timezone.utc)

    part1 = create_log_partition(day1)
    part2 = create_log_partition(day2)

    bulk_insert_logs(day1, 30)
    bulk_insert_logs(day2, 30)

    archive_and_swap_partition(part1, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)
    archive_and_swap_partition(part2, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)

    c1 = count_parquet_rows(ORG_ID, "20250501")
    c2 = count_parquet_rows(ORG_ID, "20250502")
    record("4a: Both days archived", c1 == 30 and c2 == 30, f"day1={c1}, day2={c2}")

    # Corrupt day1's parquet
    pq_path = get_parquet_path(ORG_ID, "20250501")
    with open(pq_path, "wb") as f:
        f.write(b"CORRUPT DATA")

    query_start = day1
    query_end = day2 + timedelta(days=1)
    error_raised = None
    results = None
    try:
        results = query_cold_logs(organization_id=ORG_ID, start_dt=query_start, end_dt=query_end, limit=200)
    except Exception as e:
        error_raised = e

    if error_raised:
        record(
            "4b: Corrupt parquet — unhandled exception (KNOWN LIMITATION)",
            False,
            f"{type(error_raised).__name__}: {error_raised}\n"
            "The glob-based query reads all org files together, so one corrupt\n"
            "file poisons the entire query. Per-file error handling would fix this.",
        )
    else:
        record("4b: Corrupt parquet — returns partial results",
               results is not None, f"Got {len(results)} results")

    # Delete both files, query again
    for d in ("20250501", "20250502"):
        p = get_parquet_path(ORG_ID, d)
        if os.path.exists(p):
            os.remove(p)

    results_empty = None
    error_empty = None
    try:
        results_empty = query_cold_logs(organization_id=ORG_ID, start_dt=query_start, end_dt=query_end, limit=200)
    except Exception as e:
        error_empty = e

    if error_empty:
        record("4c: All parquet deleted — exception (BUG)", False,
               f"{type(error_empty).__name__}: {error_empty}")
    else:
        record("4c: All parquet deleted — returns empty",
               results_empty is not None and len(results_empty) == 0,
               f"got {len(results_empty) if results_empty else 'None'}")


# ── Test 5: DuckDB Memory Pressure ─────────────────────────────────


def test_memory_pressure():
    logger.info("\n=== Test 5: DuckDB Memory Pressure ===")
    clean_cold_storage()

    DAYS = 365
    EVENTS_PER_DAY = 100
    base_date = datetime(2024, 6, 1, tzinfo=timezone.utc)

    logger.info(f"  Creating {DAYS} parquet files ({EVENTS_PER_DAY} events/day)...")
    t0 = time.time()
    for day in range(DAYS):
        write_parquet_directly(base_date + timedelta(days=day), EVENTS_PER_DAY)
    logger.info(f"  Created in {time.time() - t0:.1f}s")

    def measure_query(label: str, days: int):
        import resource
        gc.collect()
        mem_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t0 = time.time()
        results = query_cold_logs(
            organization_id=ORG_ID,
            start_dt=base_date,
            end_dt=base_date + timedelta(days=days),
            limit=100_000,
        )
        elapsed = time.time() - t0
        gc.collect()
        mem_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        detail = (
            f"results={len(results)}, time={elapsed:.2f}s, "
            f"peak_rss_before={mem_before}KB, peak_rss_after={mem_after}KB, "
            f"delta={mem_after - mem_before}KB"
        )
        record(f"5: Memory — {label}", True, detail)

    measure_query("30-day range", 30)
    measure_query("90-day range", 90)
    measure_query("365-day range", 365)


# ── Test 6: Concurrent Read During Write ────────────────────────────


def test_concurrent_read_write():
    logger.info("\n=== Test 6: Concurrent Read During Write ===")
    clean_cold_storage()

    # Pre-existing cold data
    pre_date = datetime(2025, 6, 30, tzinfo=timezone.utc)
    pre_part = create_log_partition(pre_date)
    bulk_insert_logs(pre_date, 100)
    archive_and_swap_partition(pre_part, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)

    # Data to be migrated concurrently
    write_date = datetime(2025, 7, 1, tzinfo=timezone.utc)
    write_part = create_log_partition(write_date)
    bulk_insert_logs(write_date, 5000)

    read_errors = []
    read_results = []

    def concurrent_reader():
        for _ in range(10):
            try:
                results = query_cold_logs(
                    organization_id=ORG_ID,
                    start_dt=pre_date,
                    end_dt=pre_date + timedelta(days=2),
                    limit=100_000,
                )
                read_results.append(len(results))
                time.sleep(0.05)
            except Exception as e:
                read_errors.append(f"{type(e).__name__}: {e}")

    def concurrent_writer():
        archive_and_swap_partition(write_part, "logs_logevent", LOG_COLUMN_TYPES, LOGS_SELECT_SQL)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader_future = pool.submit(concurrent_reader)
        writer_future = pool.submit(concurrent_writer)
        writer_future.result(timeout=120)
        reader_future.result(timeout=120)

    record("6a: No exceptions during concurrent read/write",
           len(read_errors) == 0,
           f"errors={read_errors}" if read_errors else f"{len(read_results)} reads OK")

    if read_results:
        # Each read should get either 100 (pre-migration) or 5100 (post), never partial
        valid = all(r in (100, 5100) for r in read_results)
        record("6b: Reads are consistent (no partial data)", valid,
               f"unique_counts={sorted(set(read_results))}")
    else:
        record("6b: Reads are consistent", False, "No reads completed")


# ── Test 7: Missing Parquet Files ───────────────────────────────────


def test_missing_parquet():
    logger.info("\n=== Test 7: Missing Parquet Files ===")
    clean_cold_storage()

    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 12, 31, tzinfo=timezone.utc)

    # 7a: Logs cold query
    try:
        results = query_cold_logs(organization_id=ORG_ID, start_dt=start, end_dt=end, limit=100)
        record("7a: Logs — missing parquet returns empty",
               results is not None and len(results) == 0, f"got {len(results)}")
    except Exception as e:
        record("7a: Logs — missing parquet raises (BUG)", False,
               f"{type(e).__name__}: {e}")

    # 7b: Issue events cold query
    try:
        results = query_cold_events(organization_id=ORG_ID, start_dt=start, end_dt=end)
        record("7b: Issue events — missing parquet returns empty",
               results is not None and len(results) == 0, f"got {len(results)}")
    except Exception as e:
        record("7b: Issue events — missing parquet raises (BUG)", False,
               f"{type(e).__name__}: {e}")

    # 7c: Single event lookup
    fake_time = datetime(2020, 6, 15, tzinfo=timezone.utc)
    fake_id = UUID7Helper.from_datetime(fake_time)
    try:
        result = get_event_from_cold(ORG_ID, fake_id, fake_time)
        record("7c: get_event_from_cold — missing file returns None",
               result is None, f"got {result}")
    except Exception as e:
        record("7c: get_event_from_cold — missing file raises (BUG)", False,
               f"{type(e).__name__}: {e}")


# ── Main ────────────────────────────────────────────────────────────


def main():
    logger.info("=" * 60)
    logger.info("Cold Storage E2E Test Suite")
    logger.info("=" * 60)

    if not is_duckdb_available():
        logger.error("DuckDB not available. Set GLITCHTIP_COLD_STORAGE_DIR=/tmp/cold_storage_test")
        sys.exit(1)

    storage = get_cold_storage_backend()
    if not storage:
        logger.error("No storage backend configured.")
        sys.exit(1)

    logger.info("DuckDB available: True")
    logger.info(f"Storage backend: {type(storage).__name__}")

    setup_org_and_project()

    try:
        test_migration_idempotency()
        test_migration_edge_cases()
        test_query_union_correctness()
        test_query_degradation()
        test_memory_pressure()
        test_concurrent_read_write()
        test_missing_parquet()
    except Exception:
        logger.error("Unexpected error:")
        traceback.print_exc()
    finally:
        logger.info("\n" + "=" * 60)
        logger.info("RESULTS SUMMARY")
        logger.info("=" * 60)

        passed = sum(1 for r in RESULTS if r["status"] == "PASS")
        failed = sum(1 for r in RESULTS if r["status"] == "FAIL")

        for r in RESULTS:
            icon = "✓" if r["status"] == "PASS" else "✗"
            logger.info(f"  {icon} [{r['status']}] {r['name']}")
            if r["detail"]:
                for line in r["detail"].strip().splitlines():
                    logger.info(f"           {line}")

        logger.info(f"\nTotal: {passed} passed, {failed} failed, {len(RESULTS)} total")
        clean_cold_storage()


if __name__ == "__main__":
    main()
