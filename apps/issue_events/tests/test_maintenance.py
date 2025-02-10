from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from ..maintenance import cleanup_old_issues
from ..models import Issue, IssueEvent


class MaintenanceTestCase(TestCase):
    def test_cleanup_old_issues(self):
        events = baker.make(
            "issue_events.IssueEvent", _quantity=5, _fill_optional=["issue"]
        )
        baker.make("issue_events.IssueEvent", issue=events[0].issue, _quantity=5)
        cleanup_old_issues()
        self.assertEqual(Issue.objects.count(), 5)

        IssueEvent.objects.all().delete()
        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_issues()
            self.assertEqual(Issue.objects.count(), 0)
