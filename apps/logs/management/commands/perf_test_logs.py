"""
Performance test for logs feature.

Tests:
1. Ingest throughput (bulk insert speed)
2. Query performance with various filters
3. Partition pruning effectiveness

Usage:
    ./manage.py perf_test_logs --generate 100000
    ./manage.py perf_test_logs --benchmark
    ./manage.py perf_test_logs --generate 100000 --benchmark
"""

import random
import time
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand
from django.db import connection

from apps.logs.constants import LogLevel
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.partition_manager import PartitionManager, UUID7Helper


class Command(BaseCommand):
    help = "Performance test for logs feature"

    SAMPLE_MESSAGES = [
        "User login successful for user_id={}",
        "Database query completed in {}ms",
        "API request processed: {} {}",
        "Cache miss for key: {}",
        "Background job {} completed",
        "Error processing request: {}",
        "Connection timeout after {}ms",
        "Memory usage: {}%",
        "Request rate: {} req/s",
        "Queue depth: {} items",
    ]

    SAMPLE_SERVICES = [
        "api-gateway",
        "auth-service",
        "user-service",
        "payment-service",
        "notification-service",
        "worker",
        "scheduler",
        "cache-manager",
        "database-proxy",
        "load-balancer",
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            "--generate",
            "-g",
            type=int,
            default=0,
            help="Number of logs to generate",
        )
        parser.add_argument(
            "--benchmark",
            "-b",
            action="store_true",
            help="Run query benchmarks",
        )
        parser.add_argument(
            "--days",
            "-d",
            type=int,
            default=7,
            help="Spread logs over this many days (default: 7)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10000,
            help="Batch size for inserts (default: 10000)",
        )
        parser.add_argument(
            "--explain",
            "-e",
            action="store_true",
            help="Show EXPLAIN ANALYZE for queries",
        )

    def handle(self, *args, **options):
        self.explain = options["explain"]

        # Get or create test org/project
        org, project = self._get_or_create_test_data()
        self.org = org
        self.project = project

        if options["generate"]:
            self._generate_logs(
                options["generate"],
                options["days"],
                options["batch_size"],
            )

        if options["benchmark"]:
            self._run_benchmarks()

        if not options["generate"] and not options["benchmark"]:
            self.stdout.write("Specify --generate N and/or --benchmark")

    def _get_or_create_test_data(self):
        """Get or create organization and project for testing."""
        org = Organization.objects.first()
        if not org:
            org = Organization.objects.create(name="Perf Test Org", slug="perf-test")
            self.stdout.write(f"Created organization: {org.slug}")

        project = Project.objects.filter(organization=org).first()
        if not project:
            project = Project.objects.create(
                name="Perf Test Project",
                slug="perf-test",
                organization=org,
            )
            self.stdout.write(f"Created project: {project.slug}")

        return org, project

    def _ensure_partitions(self, start_time: datetime, end_time: datetime):
        """Ensure partitions exist for the time range."""
        manager = PartitionManager()
        current = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )

        while current <= end:
            partition_name = f"logs_logevent_{current.strftime('%Y%m%d')}"
            if not manager.table_exists(partition_name):
                self.stdout.write(f"Creating partition {partition_name}")
                next_day = current + timedelta(days=1)
                manager.execute_partition_creation(
                    parent_table="logs_logevent",
                    partition_name=partition_name,
                    start_date=current,
                    end_date=next_day,
                    hash_buckets=None,
                    hash_column="organization_id",
                    key_type="uuid7",
                )
            current += timedelta(days=1)

    def _generate_logs(self, count: int, days: int, batch_size: int):
        """Generate test logs."""
        import orjson

        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"GENERATING {count:,} LOGS OVER {days} DAYS")
        self.stdout.write(f"{'=' * 60}\n")

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=days)
        end_time = now

        # Ensure partitions exist
        self._ensure_partitions(start_time, end_time)

        # Level weights
        level_weights = [
            (LogLevel.TRACE, 5),
            (LogLevel.DEBUG, 10),
            (LogLevel.INFO, 50),
            (LogLevel.WARN, 20),
            (LogLevel.ERROR, 12),
            (LogLevel.FATAL, 3),
        ]
        levels = [lw[0] for lw in level_weights]
        weights = [lw[1] for lw in level_weights]

        time_range_seconds = int((end_time - start_time).total_seconds())

        insert_sql = """
            INSERT INTO logs_logevent (
                id, trace_id, organization_id, project_id, span_id,
                level, severity_number, body, service, environment, host, data
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """

        total_inserted = 0
        batch_times = []
        start_total = time.perf_counter()

        for batch_start in range(0, count, batch_size):
            batch_end = min(batch_start + batch_size, count)
            batch_count = batch_end - batch_start
            rows = []

            for _ in range(batch_count):
                # Random timestamp
                random_offset = timedelta(seconds=random.randint(0, time_range_seconds))
                log_timestamp = start_time + random_offset

                # Generate UUIDv7
                log_id = UUID7Helper.from_datetime(log_timestamp)

                # Random values
                level = random.choices(levels, weights=weights)[0]
                service = random.choice(self.SAMPLE_SERVICES)
                message_template = random.choice(self.SAMPLE_MESSAGES)
                message = message_template.format(
                    random.randint(1, 10000),
                    random.randint(10, 5000),
                )

                # Optional trace_id
                trace_id = None
                if random.random() > 0.5:
                    trace_id = str(UUID7Helper.from_datetime(log_timestamp))

                environment = random.choice(["production", "staging", "dev"])
                host = f"server-{random.randint(1, 20)}.example.com"

                data = orjson.dumps(
                    {
                        "request_id": f"req-{random.randint(100000, 999999)}",
                    }
                ).decode("utf-8")

                rows.append(
                    (
                        str(log_id),
                        trace_id,
                        self.org.id,
                        self.project.id,
                        None,  # span_id
                        level,
                        None,  # severity_number
                        message,
                        service,
                        environment,
                        host,
                        data,
                    )
                )

            # Bulk insert
            batch_start_time = time.perf_counter()
            with connection.cursor() as cursor:
                cursor.executemany(insert_sql, rows)
            batch_time = time.perf_counter() - batch_start_time
            batch_times.append(batch_time)

            total_inserted += batch_count
            rate = batch_count / batch_time if batch_time > 0 else 0
            self.stdout.write(
                f"  Inserted {total_inserted:,}/{count:,} "
                f"({batch_time:.2f}s, {rate:,.0f} logs/sec)"
            )

        total_time = time.perf_counter() - start_total
        avg_rate = count / total_time if total_time > 0 else 0

        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write("GENERATION COMPLETE")
        self.stdout.write(f"{'=' * 60}")
        self.stdout.write(f"Total logs: {count:,}")
        self.stdout.write(f"Total time: {total_time:.2f}s")
        self.stdout.write(f"Average rate: {avg_rate:,.0f} logs/sec")
        self.stdout.write(f"Avg batch time: {sum(batch_times) / len(batch_times):.2f}s")

    def _run_benchmarks(self):
        """Run query benchmarks."""
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write("RUNNING QUERY BENCHMARKS")
        self.stdout.write(f"{'=' * 60}\n")

        # Get counts
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM logs_logevent WHERE organization_id = %s",
                [self.org.id],
            )
            total_count = cursor.fetchone()[0]

        self.stdout.write(f"Hot storage: {total_count:,} logs\n")

        # Define benchmark queries
        now = datetime.now(timezone.utc)
        benchmarks = [
            {
                "name": "Last 1 hour (hot only)",
                "start": now - timedelta(hours=1),
                "end": now,
                "filters": {},
            },
            {
                "name": "Last 24 hours (hot only)",
                "start": now - timedelta(hours=24),
                "end": now,
                "filters": {},
            },
            {
                "name": "Last 7 days (hot)",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {},
            },
            {
                "name": "Last 7 days + level=ERROR",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {"level": LogLevel.ERROR},
            },
            {
                "name": "Last 7 days + service filter",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {"service": "api-gateway"},
            },
            {
                "name": "Last 7 days + environment filter",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {"environment": "production"},
            },
            {
                "name": "Last 7 days + host filter",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {"host": "server-1.example.com"},
            },
            {
                "name": "Last 7 days + body search",
                "start": now - timedelta(days=7),
                "end": now,
                "filters": {"body_search": "Error"},
            },
            {
                "name": "Last 30 days (hot + cold)",
                "start": now - timedelta(days=30),
                "end": now,
                "filters": {},
            },
        ]

        results = []
        for bench in benchmarks:
            result = self._run_benchmark(bench)
            results.append(result)

        # Summary table
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write("BENCHMARK SUMMARY")
        self.stdout.write(f"{'=' * 60}")
        self.stdout.write(f"{'Query':<40} {'Time':>10} {'Rows':>10} {'Pruned':>8}")
        self.stdout.write("-" * 70)

        for r in results:
            pruned = "✓" if r["partitions_pruned"] else "✗"
            self.stdout.write(
                f"{r['name']:<40} {r['time_ms']:>8.1f}ms {r['row_count']:>10,} {pruned:>8}"
            )

    def _run_benchmark(self, bench: dict) -> dict:
        """Run a single benchmark query."""
        self.stdout.write(f"\n--- {bench['name']} ---")

        start_uuid, end_uuid = UUID7Helper.get_range_for_date(
            bench["start"], bench["end"]
        )

        # Build query
        where_clauses = [
            "organization_id = %s",
            "id >= %s",
            "id < %s",
        ]
        params = [self.org.id, str(start_uuid), str(end_uuid)]

        filters = bench.get("filters", {})
        if "level" in filters:
            where_clauses.append("level = %s")
            params.append(filters["level"])
        if "service" in filters:
            where_clauses.append("service = %s")
            params.append(filters["service"])
        if "environment" in filters:
            where_clauses.append("environment = %s")
            params.append(filters["environment"])
        if "host" in filters:
            where_clauses.append("host = %s")
            params.append(filters["host"])
        if "body_search" in filters:
            where_clauses.append("body ILIKE %s")
            params.append(f"%{filters['body_search']}%")

        where_sql = " AND ".join(where_clauses)

        # Count query
        count_sql = f"""
            SELECT COUNT(*) FROM logs_logevent
            WHERE {where_sql}
        """

        # Timed query with LIMIT
        select_sql = f"""
            SELECT id, level, body, service, environment, host
            FROM logs_logevent
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT 100
        """

        # Run EXPLAIN ANALYZE if requested
        partitions_pruned = True
        if self.explain:
            explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {select_sql}"
            with connection.cursor() as cursor:
                cursor.execute(explain_sql, params)
                plan = "\n".join(row[0] for row in cursor.fetchall())
                self.stdout.write(f"\nEXPLAIN ANALYZE:\n{plan}\n")

                # Check for partition pruning
                if (
                    "Seq Scan on logs_logevent" in plan
                    and "never executed" not in plan.lower()
                ):
                    # Check if scanning all partitions
                    partition_scans = plan.count("logs_logevent_")
                    if partition_scans > 10:  # Arbitrary threshold
                        partitions_pruned = False

        # Run count
        with connection.cursor() as cursor:
            cursor.execute(count_sql, params)
            row_count = cursor.fetchone()[0]

        # Run timed query
        start_time = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute(select_sql, params)
            rows = cursor.fetchall()
        query_time = time.perf_counter() - start_time

        time_ms = query_time * 1000
        self.stdout.write(f"  Rows: {row_count:,}")
        self.stdout.write(f"  Time: {time_ms:.1f}ms")
        self.stdout.write(f"  Returned: {len(rows)} rows")

        return {
            "name": bench["name"],
            "time_ms": time_ms,
            "row_count": row_count,
            "partitions_pruned": partitions_pruned,
        }
