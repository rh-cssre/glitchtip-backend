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
        events = baker.make("issues.Event", _quantity=5)
        baker.make("issues.Event", issue=events[0].issue, _quantity=5)
        cleanup_old_events()
        self.assertEqual(Event.objects.count(), 10)
        self.assertEqual(Issue.objects.count(), 5)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_events()
            self.assertEqual(Event.objects.count(), 0)
            self.assertEqual(Issue.objects.count(), 0)

    def test_cleanup_old_events_foreign_keys(self):
        event = baker.make("issues.Event")
        event_tag = baker.make("issues.EventTag")
        event.tags.add(event_tag)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_events()
            self.assertEqual(Event.objects.count(), 0)
