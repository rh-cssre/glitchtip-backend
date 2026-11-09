from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone
from freezegun import freeze_time

from apps.performance.maintenance import cleanup_old_transaction_events
from apps.performance.models import TransactionGroup
from glitchtip.test_utils.test_case import GlitchTipTestCase

_cleanup_old_transaction_events_sync = async_to_sync(cleanup_old_transaction_events)


class TasksTestCase(GlitchTipTestCase):
    def test_cleanup_old_groups(self):
        """Groups with last_seen older than retention are cleaned up."""
        self.create_logged_in_user()
        now = timezone.now()

        old_group = TransactionGroup.objects.create(
            project=self.project,
            organization=self.organization,
            transaction="/old/",
            op="http.server",
            first_seen=now - timedelta(days=100),
            last_seen=now - timedelta(days=100),
        )
        frozen_now = now + timedelta(
            days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS + 1
        )
        recent_group = TransactionGroup.objects.create(
            project=self.project,
            organization=self.organization,
            transaction="/recent/",
            op="http.server",
            first_seen=now - timedelta(days=1),
            last_seen=frozen_now,
        )

        with freeze_time(frozen_now):
            _cleanup_old_transaction_events_sync()

        # Recent group should survive, old group should be deleted
        self.assertFalse(TransactionGroup.objects.filter(id=old_group.id).exists())
        self.assertTrue(TransactionGroup.objects.filter(id=recent_group.id).exists())

    def test_cleanup_no_groups(self):
        """Cleanup runs without error when there are no groups."""
        _cleanup_old_transaction_events_sync()
        self.assertEqual(TransactionGroup.objects.count(), 0)
