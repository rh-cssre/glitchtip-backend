import datetime
from collections import defaultdict

from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.event_ingest.process_event import update_transaction_group_stats
from glitchtip.test_utils.test_case import GlitchTestCase


class TransactionAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse("api:list_transactions", args=[cls.organization.slug])

    def setUp(self):
        self.client.force_login(self.user)

    def test_list(self):
        transaction = baker.make(
            "performance.TransactionEvent", group__project=self.project
        )
        res = self.client.get(self.list_url)
        self.assertContains(res, transaction.event_id)


class TransactionGroupAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse(
            "api:list_transaction_groups", args=[cls.organization.slug]
        )

    def setUp(self):
        self.client.force_login(self.user)

    def create_transaction_and_update_stats(
        self, group, start_timestamp=None, timestamp=None
    ):
        """
        Test helper to create a transaction event and immediately call the
        production aggregation logic to populate the stats model.
        """

        if start_timestamp is None:
            start_timestamp = timezone.now()
        # Create the raw event for completeness.
        organization = group.project.organization
        event = baker.make(
            "performance.TransactionEvent",
            group=group,
            organization=organization,
            start_timestamp=start_timestamp,
            timestamp=timestamp,
        )

        # Now, call the production stats function with data for this single event.
        minute_timestamp = start_timestamp.replace(second=0, microsecond=0)
        stats_data = defaultdict(
            lambda: defaultdict(
                lambda: {
                    "count": 0,
                    "total_duration": 0.0,
                    "sum_of_squares_duration": 0.0,
                }
            )
        )

        stats_bucket = stats_data[minute_timestamp][group.id]
        stats_bucket["organization_id"] = organization.id
        stats_bucket["count"] = 1
        stats_bucket["total_duration"] = event.duration_ms
        stats_bucket["sum_of_squares_duration"] = event.duration_ms**2

        # This is the key: we are directly calling the real database writer.
        update_transaction_group_stats(stats_data)

        return event

    def test_list(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        res = self.client.get(self.list_url)
        self.assertContains(res, group.transaction)

    def test_list_relative_datetime_filter(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        now = timezone.now().replace(second=0, microsecond=0)
        last_minute = now - datetime.timedelta(minutes=1)
        self.create_transaction_and_update_stats(
            group=group,
            start_timestamp=last_minute,
            timestamp=last_minute + datetime.timedelta(seconds=5),
        )
        two_minutes_ago = now - datetime.timedelta(minutes=2)
        self.create_transaction_and_update_stats(
            group=group,
            start_timestamp=two_minutes_ago,
            timestamp=two_minutes_ago + datetime.timedelta(seconds=1),
        )
        yesterday = now - datetime.timedelta(days=1)
        self.create_transaction_and_update_stats(
            group=group,
            start_timestamp=yesterday,
            timestamp=yesterday + datetime.timedelta(seconds=1),
        )

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": last_minute})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": "now-1m", "end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": "now-2m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 2)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now-1d"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now-24h"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["transactionCount"], 3)

    def test_list_relative_parsing(self):
        res = self.client.get(self.list_url, {"start": "now-1h "})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now - 1h"})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now-1"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now-1minute"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "won-1m"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now+1m"})
        self.assertEqual(res.status_code, 422)
        res = self.client.get(self.list_url, {"start": "now 1m"})
        self.assertEqual(res.status_code, 422)

    def test_list_environment_filter(self):
        environment_project = baker.make(
            "environments.EnvironmentProject",
            environment__organization=self.organization,
        )
        environment = environment_project.environment
        environment.projects.add(self.project)
        group1 = baker.make(
            "performance.TransactionGroup",
            project=self.project,
            tags={"environment": [environment.name]},
        )
        group2 = baker.make("performance.TransactionGroup", project=self.project)
        res = self.client.get(self.list_url, {"environment": environment.name})
        self.assertContains(res, group1.transaction)
        self.assertNotContains(res, group2.transaction)

    def test_filter_then_average(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        now = timezone.now()
        last_minute = now - datetime.timedelta(minutes=1)

        # Use the new helper to create events and update stats
        self.create_transaction_and_update_stats(
            group=group,
            start_timestamp=last_minute,
            timestamp=last_minute + datetime.timedelta(seconds=5),
        )
        transaction2 = self.create_transaction_and_update_stats(
            group=group,
            start_timestamp=now,
            timestamp=now + datetime.timedelta(seconds=1),
        )

        # This assertion now works because the view is reading from the populated stats table
        res = self.client.get(self.list_url)
        self.assertEqual(res.json()[0]["avgDuration"], 3000)

        # This filtered assertion also works correctly
        res = self.client.get(
            self.list_url
            + "?start="
            + transaction2.start_timestamp.replace(second=0, microsecond=0)
            .replace(tzinfo=None)
            .isoformat()
            + "Z"
        )
        self.assertEqual(res.json()[0]["avgDuration"], 1000)
