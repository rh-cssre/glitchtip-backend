from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..maintenance import cleanup_old_transaction_events
from ..models import TransactionEvent, TransactionGroup


class TasksTestCase(GlitchTipTestCase):
    def test_cleanup_old_events(self):
        groups = baker.make("performance.TransactionGroup", _quantity=2)
        baker.make("performance.TransactionEvent", group=groups[0])
        cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 1)

        TransactionEvent.objects.all().delete()
        cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 0)
