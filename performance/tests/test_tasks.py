from datetime import timedelta
from django.conf import settings
from django.test import TestCase
from django.utils.timezone import now
from model_bakery import baker
from freezegun import freeze_time
from ..models import TransactionEvent, TransactionGroup, Span
from ..tasks import cleanup_old_transaction_events


class TasksTestCase(TestCase):
    def test_cleanup_old_events(self):
        group = baker.make("performance.TransactionGroup")
        transactions = baker.make(
            "performance.TransactionEvent", group=group, _quantity=5
        )
        baker.make("performance.Span", transaction=transactions[0])
        cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 1)
        self.assertEqual(TransactionEvent.objects.count(), 5)
        self.assertEqual(Span.objects.count(), 1)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS)
        ):
            cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 0)
        self.assertEqual(TransactionEvent.objects.count(), 0)
        self.assertEqual(Span.objects.count(), 0)
