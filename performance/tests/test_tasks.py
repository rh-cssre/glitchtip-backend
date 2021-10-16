from datetime import timedelta
from django.conf import settings
from django.test import TestCase
from django.utils.timezone import now
from model_bakery import baker
from freezegun import freeze_time
from ..models import TransactionEvent
from ..tasks import cleanup_old_transaction_events


class TasksTestCase(TestCase):
    def test_cleanup_old_events(self):
        baker.make("performance.TransactionEvent", _quantity=5)
        cleanup_old_transaction_events()
        self.assertEqual(TransactionEvent.objects.count(), 5)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_transaction_events()
        self.assertEqual(TransactionEvent.objects.count(), 0)
