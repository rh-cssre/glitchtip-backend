from collections import Counter
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import connection

from apps.organizations_ext.models import Organization
from apps.projects.models import Project


class MakeSampleCommand(BaseCommand):
    organization = None
    project = None
    batch_size = 10000

    def _ensure_partitions(
        self,
        start_time: datetime,
        end_time: datetime,
        daily_tables: list[str] | None = None,
        weekly_tables: list[str] | None = None,
    ):
        """Ensure partitions exist for the given time range."""
        from glitchtip.partition_manager import PartitionManager

        manager = PartitionManager()

        # Align to midnight to avoid overlapping with existing partitions
        start_date = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = end_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )

        if daily_tables:
            for table in daily_tables:
                manager.create_partitions_for_date_range(
                    parent_table=table,
                    start_date=start_date,
                    end_date=end_date,
                    partition_interval="DAY",
                    hash_buckets=None,
                    hash_column="organization_id",
                    key_type="uuid7",
                )

        if weekly_tables:
            start_of_week = start_date - timedelta(days=start_date.weekday())
            for table in weekly_tables:
                manager.create_partitions_for_date_range(
                    parent_table=table,
                    start_date=start_of_week,
                    end_date=end_date + timedelta(weeks=1),
                    partition_interval="WEEK",
                    hash_buckets=None,
                    hash_column="organization_id",
                    key_type="datetime",
                )

    def add_org_project_arguments(self, parser):
        parser.add_argument("--org", type=str, help="Organization slug")
        parser.add_argument("--project", type=str, help="Project slug")

    def add_arguments(self, parser):
        parser.add_argument("--quantity", type=int, default=1000)
        self.add_org_project_arguments(parser)

    def handle(self, *args, **options):
        self.organization = self.get_organization(options.get("org"))
        self.project = self.get_project(options.get("project"))

    def get_organization(self, org: str):
        if org:
            return Organization.objects.get(slug=org)

        organization = Organization.objects.first()
        if not organization:
            organization = Organization.objects.create(name="sample org")
        return organization

    def get_project(self, project: str):
        if project:
            return Project.objects.get(slug=project, organization=self.organization)
        project = Project.objects.filter(organization=self.organization).first()
        if not project:
            project = Project.objects.create(
                name="sample project", organization=self.organization
            )
        return project

    def progress_tick(self):
        self.stdout.write(self.style.NOTICE("."), ending="")

    def success_message(self, message: str):
        self.stdout.write(self.style.SUCCESS(message))

    def upsert_hourly_project_stats(
        self, table_name: str, timestamps: list[datetime]
    ):
        """
        Upsert hourly project statistics from a list of event timestamps.
        Works for IssueEventProjectHourlyStatistic,
        TransactionEventProjectHourlyStatistic, etc.
        """
        hourly_counts: Counter[datetime] = Counter()
        for ts in timestamps:
            hour = ts.replace(minute=0, second=0, microsecond=0)
            hourly_counts[hour] += 1

        if not hourly_counts:
            return

        data = [
            (hour, self.project.id, self.organization.id, count)
            for hour, count in sorted(hourly_counts.items())
        ]
        with connection.cursor() as cursor:
            args_str = ",".join(cursor.mogrify("(%s,%s,%s,%s)", row) for row in data)
            cursor.execute(
                f"INSERT INTO {table_name} (date, project_id, organization_id, count)"
                f" VALUES {args_str}"
                f" ON CONFLICT (project_id, organization_id, date)"
                f" DO UPDATE SET count = {table_name}.count + EXCLUDED.count;"
            )
