from django.utils import timezone

from apps.performance.test_data import generate_fake_transaction_group
from glitchtip.base_commands import MakeSampleCommand


class Command(MakeSampleCommand):
    help = "Create sample transaction groups for dev and demonstration purposes"

    def handle(self, *args, **options):
        super().handle(*args, **options)

        quantity = options["quantity"]
        total_count = 0

        for _ in range(quantity):
            group = generate_fake_transaction_group(self.project)
            total_count += group.count
            self.progress_tick()

        # Populate transaction stats with total count at current hour
        self.upsert_hourly_project_stats(
            "projects_transactioneventprojecthourlystatistic",
            [timezone.now()] * total_count,
        )

        self.success_message('Successfully created "%s" transaction groups' % quantity)
