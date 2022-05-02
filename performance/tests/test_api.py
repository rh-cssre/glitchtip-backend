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
