#!/usr/bin/env python
"""
Benchmark cold storage archival and cleanup performance.

Measures time and peak memory for archive_partition_per_org and
cleanup_cold_storage_for_org at various data volumes.

Usage:
    docker compose run --rm web python manage.py shell -c "exec(open('benchmarks/bench_cold_storage.py').read())"
    OR
    docker compose exec web python manage.py shell -c "exec(open('benchmarks/bench_cold_storage.py').read())"
"""

import os
import shutil
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

# Bootstrap Django if not already initialized (e.g., when run standalone)
try:
    from django.conf import settings

    _ = settings.DATABASES  # trigger lazy setup check
except Exception:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import django

    django.setup()

from django.db import connection
from django.test.utils import override_settings

from apps.logs.cold_storage import EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
from apps.logs.constants import LogLevel  # noqa: E402
from glitchtip.partition_manager import PartitionManager, UUID7Helper  # noqa: E402


def create_partition(date: datetime) -> str:
    manager = PartitionManager()
    date_str = date.strftime("%Y%m%d")
    name = f"logs_logevent_{date_str}"
    sqls = manager.create_time_partition(
        parent_table="logs_logevent",
        partition_name=name,
        start_date=date,
        end_date=date + timedelta(days=1),
        hash_buckets=None,
        hash_column="organization_id",
        key_type="uuid7",
        partition_column="id",
    )
    with connection.cursor() as cursor:
        for sql in sqls:
            cursor.execute(sql)
    return name


def drop_partition(name: str):
    with connection.cursor() as cursor:
        try:
            cursor.execute(f"ALTER TABLE logs_logevent DETACH PARTITION {name}")
        except Exception:
            connection.connection.rollback()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        except Exception:
            connection.connection.rollback()


def _make_large_json(i: int) -> str:
    """Generate ~2-4KB JSON mimicking real log data with breadcrumbs/context."""
    import json

    return json.dumps(
        {
            "request_id": f"req-{i:08x}",
            "user_id": i % 10000,
            "session_id": f"sess-{i:012x}",
            "url": f"https://app.example.com/api/v2/organizations/{i % 500}/issues/?query=is:unresolved&limit=25",
            "method": "GET",
            "status_code": 200 + (i % 5) * 100,
            "duration_ms": 150 + (i % 2000),
            "breadcrumbs": [
                {
                    "category": "http",
                    "message": f"GET /api/v2/endpoint-{j}/ [200]",
                    "timestamp": f"2026-01-15T{j:02d}:00:00Z",
                    "data": {
                        "method": "GET",
                        "status": 200,
                        "url": f"/api/v2/endpoint-{j}/",
                    },
                }
                for j in range(8)
            ],
            "tags": {
                "environment": "production",
                "server_name": f"web-{i % 16:02d}.dc1.example.com",
                "release": "v2.5.3-beta.1+build.12345",
                "transaction": "/api/v2/organizations/{org_slug}/issues/",
            },
        }
    )


def bulk_insert_logs(
    date: datetime, count: int, org_id: int, project_id: int, *, wide_rows: bool = False
):
    with connection.cursor() as cursor:
        batch_size = 5000
        for start in range(0, count, batch_size):
            end = min(start + batch_size, count)
            rows = []
            for i in range(start, end):
                offset_ms = int((i / max(count, 1)) * 86400 * 1000)
                event_time = date + timedelta(milliseconds=offset_ms)
                event_id = UUID7Helper.from_datetime(event_time)
                if wide_rows:
                    data = _make_large_json(i)
                    body = f"Error processing request {i}: connection timeout after 30000ms to upstream service api-gateway.internal:8443 while handling /api/v2/organizations/{i % 500}/issues/"
                else:
                    data = '{"request_id": "abc-123", "user_id": 42}'
                    body = f"Test log {i} with some realistic body content for benchmarking"
                rows.append(
                    cursor.mogrify(
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            str(event_id),
                            org_id,
                            project_id,
                            LogLevel.INFO,
                            9,
                            body,
                            "benchmark-service",
                            "production",
                            "host-01.example.com",
                            data,
                        ],
                    )
                )
            values = ",".join(rows)
            cursor.execute(
                "INSERT INTO logs_logevent "
                "(id, organization_id, project_id, level, severity_number, "
                "body, service, environment, host, data) VALUES " + values
            )


def ensure_org_and_project():
    """Get or create an org and project for benchmarking."""
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project

    org, _ = Organization.objects.get_or_create(
        name="Benchmark Org", defaults={"slug": "benchmark-org"}
    )
    project, _ = Project.objects.get_or_create(
        name="Benchmark Project",
        organization=org,
        defaults={"slug": "benchmark-project"},
    )
    return org, project


def bench_archive(
    row_count: int,
    cold_dir: str,
    org_id: int,
    project_id: int,
    *,
    wide_rows: bool = False,
):
    """Benchmark archive_partition_per_org for a given row count."""
    from glitchtip.cold_storage import archive_partition_per_org

    date = datetime(2024, 1, 15, tzinfo=timezone.utc)
    part_name = create_partition(date)

    try:
        print(f"  Inserting {row_count:,} rows{' (wide)' if wide_rows else ''}...")
        bulk_insert_logs(date, row_count, org_id, project_id, wide_rows=wide_rows)

        tracemalloc.start()
        t0 = time.perf_counter()

        archived = archive_partition_per_org(
            part_name,
            "20240115",
            "logs_logevent",
            EXPORT_COLUMN_TYPES,
            LOGS_SELECT_SQL,
        )

        elapsed = time.perf_counter() - t0
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"  Archived {len(archived)} org files in {elapsed:.2f}s")
        print(f"  Peak memory: {peak / 1024 / 1024:.1f} MB")
        print(f"  Rows/sec: {row_count / elapsed:,.0f}")

        # Verify parquet files are readable
        if archived:
            import glob as globmod

            import duckdb

            from glitchtip.cold_storage import get_cold_storage_backend

            storage = get_cold_storage_backend()
            _, rel_path = archived[0]
            abs_path = storage.path(rel_path)
            # Flat file or chunk directory — find all parquet files
            chunk_dir = abs_path.replace(".parquet", "")
            parquet_files = globmod.glob(f"{chunk_dir}/chunk_*.parquet")
            if not parquet_files:
                parquet_files = [abs_path]
            duck = duckdb.connect()
            count = duck.execute(
                f"SELECT COUNT(*) FROM read_parquet({parquet_files!r})"
            ).fetchone()[0]
            duck.close()
            print(f"  Parquet row count: {count:,} ({len(parquet_files)} file(s))")
            assert count == row_count, f"Expected {row_count}, got {count}"

        return elapsed, peak
    finally:
        drop_partition(part_name)


def bench_cleanup(num_files: int, cold_dir: str, org_id: int):
    """Benchmark cleanup_cold_storage_for_org with N pre-existing files."""
    from glitchtip.cold_storage import cleanup_cold_storage_for_org

    # Create fake parquet files to clean up
    org_prefix = os.path.join(
        cold_dir, "cold_storage", "logs_logevent", f"org_{org_id}"
    )
    os.makedirs(org_prefix, exist_ok=True)

    cutoff_days = 90
    for i in range(num_files):
        # Files that are older than retention
        file_date = datetime.now() - timedelta(days=cutoff_days + 1 + i)
        date_str = file_date.strftime("%Y%m%d")
        filepath = os.path.join(org_prefix, f"{date_str}.parquet")
        with open(filepath, "wb") as f:
            f.write(b"fake parquet data")

    # Also create some files that should NOT be deleted (recent)
    for i in range(5):
        file_date = datetime.now() - timedelta(days=i)
        date_str = file_date.strftime("%Y%m%d")
        filepath = os.path.join(org_prefix, f"{date_str}.parquet")
        with open(filepath, "wb") as f:
            f.write(b"recent parquet data")

    tracemalloc.start()
    t0 = time.perf_counter()

    deleted = cleanup_cold_storage_for_org(org_id, cutoff_days, "logs_logevent")

    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Verify recent files were kept
    remaining = [f for f in os.listdir(org_prefix) if f.endswith(".parquet")]

    print(f"  Deleted {deleted} / {num_files + 5} files in {elapsed:.4f}s")
    print(f"  Remaining (recent): {len(remaining)}")
    print(f"  Peak memory: {peak / 1024:.1f} KB")

    # New code deletes all expired files; old code was capped at ~29 (30-day window).
    # Don't assert exact counts since this benchmark runs against both versions.

    return elapsed, peak


def bench_cleanup_all(num_orgs: int, files_per_org: int, cold_dir: str):
    """Benchmark cleanup_all_cold_storage with many orgs."""
    from glitchtip.cold_storage import cleanup_all_cold_storage

    # Create directory structure for N orgs
    cutoff_days = 90
    for org_num in range(1, num_orgs + 1):
        org_prefix = os.path.join(
            cold_dir, "cold_storage", "logs_logevent", f"org_{org_num}"
        )
        os.makedirs(org_prefix, exist_ok=True)
        for i in range(files_per_org):
            file_date = datetime.now() - timedelta(days=cutoff_days + 1 + i)
            date_str = file_date.strftime("%Y%m%d")
            with open(os.path.join(org_prefix, f"{date_str}.parquet"), "wb") as f:
                f.write(b"fake")

    tracemalloc.start()
    t0 = time.perf_counter()

    deleted = cleanup_all_cold_storage(
        retention_days=cutoff_days, table_name="logs_logevent"
    )

    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(
        f"  {num_orgs} orgs x {files_per_org} files = {num_orgs * files_per_org} total"
    )
    print(f"  Deleted {deleted} files in {elapsed:.3f}s")
    print(f"  Peak memory: {peak / 1024:.1f} KB")

    return elapsed, peak


def main():
    org, project = ensure_org_and_project()
    cold_dir = tempfile.mkdtemp(prefix="bench_cold_")

    print(f"Cold storage dir: {cold_dir}")
    print(f"Org: {org.id}, Project: {project.id}")
    print()

    with override_settings(
        GLITCHTIP_ENABLE_DUCKDB="true",
        GLITCHTIP_COLD_STORAGE_DIR=cold_dir,
        GLITCHTIP_COLD_STORAGE_BUCKET=None,
        AWS_STORAGE_BUCKET_NAME=None,
        BILLING_ENABLED=False,
        GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED=True,
    ):
        # Benchmark archival at different scales
        print("=" * 60)
        print("ARCHIVE BENCHMARK — small rows (~150 bytes/row)")
        print("=" * 60)
        for count in [1_000, 10_000, 50_000, 200_000]:
            print(f"\n--- {count:,} rows ---")
            bench_archive(count, cold_dir, org.id, project.id)
            # Clean up cold files between runs
            shutil.rmtree(cold_dir, ignore_errors=True)
            os.makedirs(cold_dir)

        print()
        print("=" * 60)
        print("ARCHIVE BENCHMARK — wide rows (~3KB/row, realistic JSON)")
        print("=" * 60)
        for count in [1_000, 10_000, 50_000]:
            print(f"\n--- {count:,} wide rows ---")
            bench_archive(count, cold_dir, org.id, project.id, wide_rows=True)
            shutil.rmtree(cold_dir, ignore_errors=True)
            os.makedirs(cold_dir)

        # Benchmark cleanup at different scales
        print()
        print("=" * 60)
        print("CLEANUP BENCHMARK (cleanup_cold_storage_for_org)")
        print("=" * 60)
        for num_files in [10, 30, 100]:
            print(f"\n--- {num_files} files ---")
            shutil.rmtree(cold_dir, ignore_errors=True)
            os.makedirs(cold_dir)
            bench_cleanup(num_files, cold_dir, org.id)

        # Benchmark cleanup_all with many orgs
        print()
        print("=" * 60)
        print("CLEANUP ALL BENCHMARK (cleanup_all_cold_storage)")
        print("=" * 60)
        for num_orgs, files_per in [(10, 10), (100, 10), (1000, 5)]:
            print(f"\n--- {num_orgs} orgs x {files_per} files ---")
            shutil.rmtree(cold_dir, ignore_errors=True)
            os.makedirs(cold_dir)
            bench_cleanup_all(num_orgs, files_per, cold_dir)

    shutil.rmtree(cold_dir, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
else:
    # Support: exec(open('benchmarks/bench_cold_storage.py').read())
    main()
