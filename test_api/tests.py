from django.test import TestCase
from django.urls import reverse

from apps.issue_events.models import Issue
from apps.uptime.models import Monitor
from apps.users.models import User


class TestAPITestCase(TestCase):
    def test_seed_data(self):
        with self.settings(ENABLE_TEST_API=True):
            url = reverse("seed_data")
            res = self.client.post(
                url,
                QUERY_STRING="extras=true&seedIssues=true"
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(User.objects.all().count(), 2)
        self.assertEqual(Issue.objects.all().count(), 55)

        monitor = Monitor.objects.all().first()
        self.assertEqual(monitor.name, "seeded-monitor")

    def test_disabled_test_api(self):
        with self.settings(ENABLE_TEST_API=False):
            url = reverse("seed_data")
            res = self.client.post(url)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(User.objects.all().count(), 0)
