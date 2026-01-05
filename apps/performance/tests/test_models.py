from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from model_bakery import baker


class PerformanceModelTestCase(TestCase):
    def test_transaction_duration(self):
        now = timezone.now()
        transaction = baker.make(
            "performance.TransactionEvent",
            start_timestamp=now,
            timestamp=now + timedelta(seconds=1),
        )
        self.assertEqual(transaction.duration, timedelta(seconds=1))
        self.assertEqual(transaction.duration_ms, 1000)

        # Do not allow negative durations
        transaction = baker.make(
            "performance.TransactionEvent",
            start_timestamp=now,
            timestamp=now - timedelta(seconds=1),
        )
        self.assertEqual(transaction.duration, timedelta(seconds=0))
        self.assertEqual(transaction.duration_ms, 0)
