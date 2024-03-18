from django.utils import timezone

from apps.performance.models import TransactionEvent
from apps.performance.test_data import generate_fake_transaction_event
from apps.projects.models import TransactionEventProjectHourlyStatistic
from glitchtip.base_commands import MakeSampleCommand


class Command(MakeSampleCommand):
    help = (
        "Create a large number of transaction events for dev and demonstration purposes"
    )

    def handle(self, *args, **options):
        super().handle(*args, **options)

        quantity = options["quantity"]
        batch_size = self.batch_size

        if quantity < batch_size:
            batches = 1
        else:
            batches = quantity // batch_size

        for _ in range(batches):
            if quantity < batch_size:
                batch_size = quantity
            event_list = []
            for _ in range(batch_size):
                event = generate_fake_transaction_event(self.project, timezone.now())
                event_list.append(event)
            TransactionEvent.objects.bulk_create(event_list)
            quantity -= batch_size
            self.progress_tick()

        TransactionEventProjectHourlyStatistic.update(self.project.pk, timezone.now())

        self.success_message(
            'Successfully created "%s" transaction events' % options["quantity"]
        )
