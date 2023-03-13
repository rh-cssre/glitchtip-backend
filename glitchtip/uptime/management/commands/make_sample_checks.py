from random import randrange

from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug

from glitchtip.base_commands import MakeSampleCommand
from glitchtip.uptime.models import Monitor, MonitorCheck
from organizations_ext.models import Organization
from projects.models import Project

baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("django.db.models.JSONField", gen_json)


class Command(MakeSampleCommand):
    help = "Create a number of monitors each with checks for dev and demonstration purposes."

    def add_arguments(self, parser):
        self.add_org_project_arguments(parser)
        parser.add_argument("--monitor-quantity", type=int, default=10)
        parser.add_argument("--checks-quantity-per", type=int, default=100)

    def handle(self, *args, **options):
        super().handle(*args, **options)

        monitor_quantity = options["monitor_quantity"]
        checks_quantity_per = options["checks_quantity_per"]
        for x in range(monitor_quantity):
            monitor = Monitor.objects.create(
                project=self.project,
                name=f"Test Monitor #{x}",
                organization=self.organization,
                url="https://example.com",
                interval="60",
                monitor_type="Ping",
                expected_status="200",
            )

            checks = []
            for y in range(checks_quantity_per):
                with freeze_time(timezone.now() - timezone.timedelta(minutes=y)):
                    checks.append(
                        MonitorCheck(
                            monitor=monitor,
                            is_up=True,
                            start_check=timezone.now() - timezone.timedelta(minutes=y),
                            response_time=timezone.timedelta(
                                milliseconds=randrange(1, 5000)
                            ),
                        )
                    )
            MonitorCheck.objects.bulk_create(checks)
            self.progress_tick()

        self.success_message('Successfully created "%s" monitors' % monitor_quantity)
