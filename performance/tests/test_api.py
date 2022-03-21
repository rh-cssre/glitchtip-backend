from django.shortcuts import reverse
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

    def test_filter_then_average(self):
        group = baker.make("performance.TransactionGroup", project=self.project)
        with freeze_time("2022-01-01"):
            transaction1 = baker.make(
                "performance.TransactionEvent", group=group, timestamp="2022-01-01"
            )
        transaction2 = baker.make(
            "performance.TransactionEvent", group=group, timestamp="2022-01-01"
        )
        import ipdb

        ipdb.set_trace()


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
