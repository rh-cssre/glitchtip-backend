import datetime
from django.shortcuts import reverse
from django.utils import timezone
from model_bakery import baker
from freezegun import freeze_time
from glitchtip.test_utils.test_case import GlitchTipTestCase


class TransactionAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.list_url = reverse(
            "organization-transactions-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def test_list(self):
        transaction = baker.make(
            "performance.TransactionEvent", group__project=self.project
        )
        res = self.client.get(self.list_url)
        self.assertContains(res, transaction.event_id)


class TransactionGroupAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.list_url = reverse(
            "organization-transaction-groups-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def test_list(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        res = self.client.get(self.list_url)
        self.assertContains(res, group.transaction)

    def test_list_relative_datetime_filter(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        now = timezone.now()
        last_minute = now - datetime.timedelta(minutes=1)
        with freeze_time(last_minute):
            baker.make(
                "performance.TransactionEvent",
                group=group,
                start_timestamp=last_minute,
                timestamp=last_minute + datetime.timedelta(seconds=5),
                duration=datetime.timedelta(seconds=5),
            )
        two_minutes_ago = now - datetime.timedelta(minutes=2)
        with freeze_time(two_minutes_ago):
            baker.make(
                "performance.TransactionEvent",
                group=group,
                start_timestamp=two_minutes_ago,
                timestamp=two_minutes_ago + datetime.timedelta(seconds=1),
                duration=datetime.timedelta(seconds=1),
            )

        yesterday = now - datetime.timedelta(days=1)
        with freeze_time(yesterday):
            baker.make(
                "performance.TransactionEvent",
                group=group,
                start_timestamp=yesterday,
                timestamp=yesterday + datetime.timedelta(seconds=1),
                duration=datetime.timedelta(seconds=1),
            )

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": last_minute})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": "now-1m", "end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"start": "now-2m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 2)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now-1d"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now-24h"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 1)

        with freeze_time(now):
            res = self.client.get(self.list_url, {"end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data[0]["transactionCount"], 3)

    def test_list_relative_parsing(self):
        res = self.client.get(self.list_url, {"start": "now-1h "})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now - 1h"})
        self.assertEqual(res.status_code, 200)
        res = self.client.get(self.list_url, {"start": "now-1"})
        self.assertEqual(res.status_code, 400)
        res = self.client.get(self.list_url, {"start": "now-1minute"})
        self.assertEqual(res.status_code, 400)
        res = self.client.get(self.list_url, {"start": "won-1m"})
        self.assertEqual(res.status_code, 400)
        res = self.client.get(self.list_url, {"start": "now+1m"})
        self.assertEqual(res.status_code, 400)
        res = self.client.get(self.list_url, {"start": "now 1m"})
        self.assertEqual(res.status_code, 400)

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
        with freeze_time(last_minute):
            baker.make(
                "performance.TransactionEvent",
                group=group,
                start_timestamp=last_minute,
                timestamp=last_minute + datetime.timedelta(seconds=5),
                duration=datetime.timedelta(seconds=5),
            )
        transaction2 = baker.make(
            "performance.TransactionEvent",
            group=group,
            start_timestamp=now,
            timestamp=now + datetime.timedelta(seconds=1),
            duration=datetime.timedelta(seconds=1),
        )
        res = self.client.get(self.list_url)
        self.assertEqual(res.data[0]["avgDuration"], "00:00:03")

        res = self.client.get(
            self.list_url
            + "?start="
            + transaction2.created.replace(microsecond=0)
            .replace(tzinfo=None)
            .isoformat()
            + "Z"
        )
        self.assertEqual(res.data[0]["avgDuration"], "00:00:01")


class SpanAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.list_url = reverse(
            "organization-spans-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def test_list(self):
        span = baker.make("performance.Span", transaction__group__project=self.project)
        res = self.client.get(self.list_url)
        self.assertContains(res, span.op)
