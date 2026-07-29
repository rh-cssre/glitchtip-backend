from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.issue_events.models import Issue
from apps.organizations_ext.models import Organization
from apps.uptime.models import Monitor
from apps.users.models import User


class TestAPITestCase(TestCase):
    def test_seed_data(self):
        with self.settings(ENABLE_TEST_API=True):
            url = reverse("seed_data")
            res = self.client.post(url, QUERY_STRING="extras=true&seedIssues=true")
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


class SeedDataRepeatTestCase(TransactionTestCase):
    """
    Every e2e spec seeds in `beforeEach`, so seeding must stay repeatable.
    TransactionTestCase so the deferred organization_has_members trigger
    actually fires on commit.
    """

    def test_seed_data_is_repeatable(self):
        with self.settings(ENABLE_TEST_API=True):
            url = reverse("seed_data")
            self.assertEqual(self.client.post(url).status_code, 200)
            self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Organization.objects.count(), 1)

    def test_seed_data_replaces_untagged_org(self):
        """An org left by a test without `e2etestobj` in its name still holds
        the seeded user, and must not wedge the next seed."""
        with self.settings(ENABLE_TEST_API=True):
            url = reverse("seed_data")
            self.assertEqual(self.client.post(url).status_code, 200)
            Organization.objects.update(name="untagged-org", slug="untagged-org")
            self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(
            list(Organization.objects.values_list("name", flat=True)),
            ["e2etestobj-seeded-org"],
        )
