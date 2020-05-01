from datetime import timedelta
from django.conf import settings
from django.test import TestCase
from django.utils.timezone import now
from model_bakery import baker
from freezegun import freeze_time
from issues.models import Event, Issue
from ..tasks import cleanup_old_events


class TasksTestCase(TestCase):
    def test_cleanup_old_events(self):
        baker.make("issues.Event")
        cleanup_old_events()
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(Issue.objects.count(), 1)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_events()
            self.assertEqual(Event.objects.count(), 0)
            self.assertEqual(Issue.objects.count(), 0)
