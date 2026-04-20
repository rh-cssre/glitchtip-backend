import random
from collections import Counter
from datetime import datetime, timedelta, timezone

from django.db import connection

from apps.logs.constants import LogLevel
from apps.logs.models import LogResource, compute_hash_bucket
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.partition_manager import PartitionManager, UUID7Helper


class Command(MakeSampleCommand):
    help = "Generate sample log events for testing"

    SAMPLE_MESSAGES = [
        "User login successful",
        "Database connection established",
        "Request processed in {ms}ms",
        "Cache miss for key: {key}",
        "API rate limit reached for user {user_id}",
        "Background job completed: {job_name}",
        "Email sent to {email}",
        "Payment processed: ${amount}",
        "File uploaded: {filename}",
        "Session expired for user {user_id}",
        "Configuration reloaded",
        "Health check passed",
        "Memory usage: {percent}%",
        "Queue length: {count} items",
        "Connection timeout to {service}",
        "Invalid request body received",
        "Authentication failed for user {username}",
        "Database query slow: {query_time}ms",
        "Certificate expiring in {days} days",
        "Deployment completed successfully",
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
    ]

    SAMPLE_ENVIRONMENTS = ["production", "staging", "development", "testing"]
    SAMPLE_HOSTS = [f"web-{i}.example.com" for i in range(1, 6)] + [
        f"worker-{i}.example.com" for i in range(1, 4)
    ]

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--days-ago",
            "-d",
            type=int,
            default=0,
            help="Generate logs starting this many days ago (default: 0, today)",
        )
        parser.add_argument(
            "--span-days",
            "-s",
            type=int,
            default=1,
            help="Distribute logs over this many days (default: 1)",
        )

    def handle(self, *args, **options):
        super().handle(*args, **options)
        quantity = options["quantity"]
        days_ago = options["days_ago"]
        span_days = options["span_days"]

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=days_ago + span_days)
        end_time = now - timedelta(days=days_ago)

        self.stdout.write(
            f"Generating {quantity} log events for "
            f"{self.organization.slug}/{self.project.slug}"
        )
        self.stdout.write(f"Time range: {start_time} to {end_time}")

        self._ensure_partitions(start_time, end_time)

        logs_created, log_stats = self._bulk_create_logs(quantity, start_time, end_time)
        self._upsert_log_stats(log_stats)
        self._ensure_log_resources()

        self.success_message(f"Successfully created {logs_created} log events")

    def _ensure_partitions(self, start_time: datetime, end_time: datetime):
        """Ensure partitions exist for the given time range."""
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

    def _bulk_create_logs(
        self,
        quantity: int,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[int, Counter]:
        """Bulk create log events using raw SQL for performance.
        Returns (count, stats_counter) where stats_counter keys are
        (hour, level, service_bucket, env_bucket)."""
        import orjson

        time_range_seconds = int((end_time - start_time).total_seconds())

        level_weights = [
            (LogLevel.TRACE, 5),
            (LogLevel.DEBUG, 10),
            (LogLevel.INFO, 40),
            (LogLevel.WARN, 25),
            (LogLevel.ERROR, 15),
            (LogLevel.FATAL, 5),
        ]
        levels = [lw[0] for lw in level_weights]
        weights = [lw[1] for lw in level_weights]

        rows = []
        stats: Counter[tuple] = Counter()
        for i in range(quantity):
            random_offset = timedelta(seconds=random.randint(0, time_range_seconds))
            log_timestamp = start_time + random_offset

            log_id = UUID7Helper.from_datetime(log_timestamp)
            level = random.choices(levels, weights=weights)[0]

            message_template = random.choice(self.SAMPLE_MESSAGES)
            message = message_template.format(
                ms=random.randint(10, 500),
                key=f"user:{random.randint(1000, 9999)}",
                user_id=random.randint(1, 1000),
                job_name=f"process_batch_{random.randint(1, 100)}",
                email=f"user{random.randint(1, 100)}@example.com",
                amount=random.randint(10, 1000),
                filename=f"upload_{random.randint(1000, 9999)}.pdf",
                username=f"user{random.randint(1, 100)}",
                percent=random.randint(50, 99),
                count=random.randint(1, 100),
                service=random.choice(self.SAMPLE_SERVICES),
                query_time=random.randint(100, 5000),
                days=random.randint(1, 30),
            )

            service = random.choice(self.SAMPLE_SERVICES)
            environment = random.choice(self.SAMPLE_ENVIRONMENTS)
            host = random.choice(self.SAMPLE_HOSTS)

            trace_id = None
            if random.random() > 0.5:
                trace_id = str(UUID7Helper.from_datetime(log_timestamp))

            data = orjson.dumps(
                {
                    "request_id": f"req-{random.randint(10000, 99999)}",
                    "user_id": random.randint(1, 1000),
                }
            ).decode("utf-8")

            rows.append(
                (
                    str(log_id),
                    trace_id,
                    self.organization.id,
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

            hour = log_timestamp.replace(minute=0, second=0, microsecond=0)
            stats[(hour, level, compute_hash_bucket(service), compute_hash_bucket(environment))] += 1

            if (i + 1) % 1000 == 0:
                self.progress_tick()

        insert_sql = """
            INSERT INTO logs_logevent (
                id, trace_id,
                organization_id, project_id, span_id,
                level, severity_number,
                body, service, environment, host, data
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """

        self.stdout.write(f"Inserting {len(rows)} logs...")
        with connection.cursor() as cursor:
            cursor.executemany(insert_sql, rows)

        return len(rows), stats

    def _upsert_log_stats(self, stats: Counter):
        """Upsert LogProjectHourlyStatistic from collected stats."""
        if not stats:
            return
        data = [
            (hour, self.project.id, self.organization.id, level, svc, env, count)
            for (hour, level, svc, env), count in sorted(stats.items())
        ]
        with connection.cursor() as cursor:
            args_str = ",".join(
                cursor.mogrify("(%s,%s,%s,%s,%s,%s,%s)", row) for row in data
            )
            cursor.execute(
                "INSERT INTO projects_logprojecthourlystatistic"
                " (date, project_id, organization_id, level, service_bucket, environment_bucket, count)"
                f" VALUES {args_str}"
                " ON CONFLICT (project_id, organization_id, date, level, service_bucket, environment_bucket)"
                " DO UPDATE SET count = projects_logprojecthourlystatistic.count + EXCLUDED.count;"
            )

    def _ensure_log_resources(self):
        """Create LogResource entries for sample services, environments, and hosts."""
        for name in self.SAMPLE_SERVICES:
            LogResource.objects.update_or_create(
                organization=self.organization,
                name=name,
                type=LogResource.ResourceType.SERVICE,
            )
        for name in self.SAMPLE_ENVIRONMENTS:
            LogResource.objects.update_or_create(
                organization=self.organization,
                name=name,
                type=LogResource.ResourceType.ENVIRONMENT,
            )
        for name in self.SAMPLE_HOSTS:
            LogResource.objects.update_or_create(
                organization=self.organization,
                name=name,
                type=LogResource.ResourceType.HOST,
            )
