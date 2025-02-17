from random import randrange

from django.utils import timezone

from apps.uptime.models import Monitor, MonitorCheck
from glitchtip.base_commands import MakeSampleCommand


class Command(MakeSampleCommand):
    help = "Create a number of monitors each with checks for dev and demonstration purposes."

    def add_arguments(self, parser):
        self.add_org_project_arguments(parser)
        parser.add_argument("--monitor-quantity", type=int, default=10)
        parser.add_argument("--checks-quantity-per", type=int, default=100)
        parser.add_argument("--first-check-down", type=bool, default=False)

    def handle(self, *args, **options):
        super().handle(*args, **options)

        monitor_quantity = options["monitor_quantity"]
        checks_quantity_per = options["checks_quantity_per"]
        first_check_down = options["first_check_down"]

        monitors = [
            Monitor(
                project=self.project,
                name=f"Test Monitor #{i}",
                organization=self.organization,
                url="https://example.com",
                interval="60",
                monitor_type="Ping",
                expected_status="200",
            )
            for i in range(monitor_quantity)
        ]
        Monitor.objects.bulk_create(monitors)

        # Create checks sequentially based on time
        # Creates a better representation of data on disk
        checks = []
        start_time = timezone.now() - timezone.timedelta(minutes=checks_quantity_per)
        for time_i in range(checks_quantity_per):
            for monitor in monitors:
                is_first = time_i == 0
                is_up = True
                if first_check_down and is_first:
                    is_up = False
                checks.append(
                    MonitorCheck(
                        monitor=monitor,
                        is_up=is_up,
                        is_change=is_first,
                        start_check=start_time + timezone.timedelta(minutes=time_i),
                        response_time=randrange(1, 5000)
                    )
                )
            if len(checks) > 10000:
                MonitorCheck.objects.bulk_create(checks)
                self.progress_tick()
                checks = []
        if checks:
            MonitorCheck.objects.bulk_create(checks)

        self.success_message('Successfully created "%s" monitors' % monitor_quantity)
