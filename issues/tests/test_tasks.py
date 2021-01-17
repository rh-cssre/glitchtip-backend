from datetime import timedelta
from django.conf import settings
from django.test import TestCase
from django.utils.timezone import now
from model_bakery import baker
from freezegun import freeze_time
from events.models import Event
from issues.models import Issue
from ..tasks import cleanup_old_events


class TasksTestCase(TestCase):
    def test_cleanup_old_events(self):
        events = baker.make("events.Event", _quantity=5, _fill_optional=["issue"])
        baker.make("events.Event", issue=events[0].issue, _quantity=5)
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
        baker.make("events.Event", tags={"foo": "bar"})

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_events()
            self.assertEqual(Event.objects.count(), 0)
