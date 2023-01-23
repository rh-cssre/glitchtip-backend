from django.core.management.base import BaseCommand
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug

from glitchtip.uptime.models import Monitor, MonitorCheck
from organizations_ext.models import Organization
from projects.models import Project

baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("django.db.models.JSONField", gen_json)


class Command(BaseCommand):
    help = "Create a number of monitors with 1,000 checks each for dev and demonstration purposes."

    def add_arguments(self, parser):
        parser.add_argument("monitor_number", nargs="?", type=int, default=10)

    def handle(self, *args, **options):
        organization = Organization.objects.first()
        if not organization:
            organization = baker.make("organizations_ext.Organization")
        project = Project.objects.filter(organization=organization).first()
        if not project:
            project = baker.make("projects.Project", organization=organization)

        monitor_number = options["monitor_number"]
        for x in range(monitor_number):
            monitor = Monitor.objects.create(
                project=project,
                name=f"Test Monitor #{x}",
                organization=organization,
                url="https://example.com",
                interval="60",
                monitor_type="Ping",
                expected_status="200",
            )

            for y in range(1000):
                with freeze_time(timezone.now() - timezone.timedelta(minutes=y)):
                    MonitorCheck.objects.create(
                        monitor=monitor,
                        is_up=True,
                        start_check=timezone.now() - timezone.timedelta(minutes=y),
                        response_time=timezone.timedelta(milliseconds=60),
                    )

        self.stdout.write(
            self.style.SUCCESS('Successfully created "%s" monitors' % monitor_number)
        )
