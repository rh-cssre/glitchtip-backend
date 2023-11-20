import random

from django.core import management
from django.test import TestCase

from events.models import Event
from issues.models import Issue


class CommandsTestCase(TestCase):
    def setUp(self):
        random.seed(32423423433)

    def test_make_sample_issues(self):
        management.call_command("make_sample_issues", issue_quantity=1)
        self.assertEqual(Issue.objects.all().count(), 1)

    def test_make_sample_issues_multiple(self):
        management.call_command(
            "make_sample_issues", issue_quantity=2, events_quantity_per=2
        )
        self.assertEqual(Issue.objects.all().count(), 2)
        self.assertEqual(Event.objects.all().count(), 4)

    def test_make_sample_events(self):
        management.call_command(
            "make_sample_events",
            quantity=2,
        )
        self.assertEqual(Event.objects.all().count(), 2)
