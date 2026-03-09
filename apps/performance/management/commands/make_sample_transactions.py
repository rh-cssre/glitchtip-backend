from apps.performance.test_data import generate_fake_transaction_group
from glitchtip.base_commands import MakeSampleCommand


class Command(MakeSampleCommand):
    help = "Create sample transaction groups for dev and demonstration purposes"

    def handle(self, *args, **options):
        super().handle(*args, **options)

        quantity = options["quantity"]

        for _ in range(quantity):
            generate_fake_transaction_group(self.project)
            self.progress_tick()

        self.success_message('Successfully created "%s" transaction groups' % quantity)
