from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import connection

from apps.organizations_ext.models import Organization
from apps.projects.models import Project


class MakeSampleCommand(BaseCommand):
    organization = None
    project = None
    batch_size = 10000

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
