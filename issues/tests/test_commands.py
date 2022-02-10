import random

from django.core import management
from django.test import TestCase

from events.models import Event


class CommandsTestCase(TestCase):
    def setUp(self):
        random.seed(32423423433)

    def test_make_sample_issues(self):
        """ Default is one random event """
        management.call_command("make_sample_issues")
        self.assertEqual(Event.objects.all().count(), 1)

    def test_make_bulk_events(self):
        management.call_command("make_bulk_events", quantity=2)
        self.assertEqual(Event.objects.all().count(), 2)

    def test_make_sample_issues_multiple(self):
        """ Default is one random event """
        management.call_command("make_sample_issues", quantity=10)
        self.assertEqual(Event.objects.all().count(), 10)

    def test_make_sample_issues_real(self):
        """ Default is one random event """
        management.call_command("make_sample_issues", only_real=True, quantity=2)
        self.assertEqual(Event.objects.all().count(), 2)

    def test_make_sample_issues_fake(self):
        """ Default is one random event """
        management.call_command("make_sample_issues", only_fake=True)
        self.assertEqual(Event.objects.all().count(), 1)
