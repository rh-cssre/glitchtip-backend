from unittest import skipIf

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.enums import BillingScheme
from freezegun import freeze_time
from model_bakery import baker


class SubscriptionAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make("organizations_ext.Organization")
        cls.organization.add_user(cls.user)
        cls.url = reverse("api:create_subscription")

    def setUp(self):
        self.client.force_login(self.user)

    def test_detail(self):
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        subscription = baker.make(
            "djstripe.Subscription",
            status="active",
            customer=customer,
            livemode=False,
            created=timezone.make_aware(timezone.datetime(2020, 1, 2)),
        )
        # Should get most recent
        baker.make(
            "djstripe.Subscription",
            status="active",
            customer=customer,
            livemode=False,
            created=timezone.make_aware(timezone.datetime(2020, 1, 1)),
        )
        # should not get canceled subscriptions
        baker.make(
            "djstripe.Subscription",
            status="canceled",
            customer=customer,
            livemode=False,
            created=timezone.make_aware(timezone.datetime(2020, 1, 3)),
        )
        baker.make("djstripe.Subscription")
        url = reverse("api:get_subscription", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, subscription.id)

    def test_events_count(self):
        """
        Event count should be accurate and work when there are multiple subscriptions for a given customer
        """
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            current_period_start=timezone.make_aware(timezone.datetime(2020, 1, 2)),
            current_period_end=timezone.make_aware(timezone.datetime(2020, 2, 2)),
        )
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            status="Cancelled",
            current_period_start=timezone.make_aware(timezone.datetime(2019, 1, 2)),
            current_period_end=timezone.make_aware(timezone.datetime(2019, 2, 2)),
        )
        url = reverse(
            "api:get_subscription_events_count", args=[self.organization.slug]
        )
        with freeze_time(timezone.datetime(2020, 3, 1)):
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=self.organization,
            )
        with freeze_time(timezone.datetime(2020, 1, 5)):
            baker.make("issue_events.IssueEvent")
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=self.organization,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=self.organization,
                count=1,
            )
            baker.make(
                "performance.TransactionEvent",
                group__project__organization=self.organization,
            )
            baker.make(
                "projects.TransactionEventProjectHourlyStatistic",
                project__organization=self.organization,
                count=1,
            )
            baker.make(
                "sourcecode.DebugSymbolBundle",
                file__blob__size=1000000,
                organization=self.organization,
                release__organization=self.organization,
                _quantity=2,
            )
        res = self.client.get(url)
        self.assertEqual(
            res.json(),
            {
                "eventCount": 1,
                "fileSizeMB": 2,
                "transactionEventCount": 1,
                "uptimeCheckEventCount": 0,
            },
        )

    def test_events_count_without_customer(self):
        """
        Due to async nature of Stripe integration, a customer may not exist
        """
        baker.make("djstripe.Subscription", livemode=False)
        url = reverse(
            "api:get_subscription_events_count", args=[self.organization.slug]
        )
        res = self.client.get(url)
        self.assertEqual(sum(res.json().values()), 0)

    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_create_free(self):
        """
        Users should not be able to create a free subscription if they have another non-canceled subscription
        """
        price = baker.make(
            "djstripe.Price",
            unit_amount=0,
            id="price_1KO6e1J4NuO0bv3IEXhpWpzt",
            billing_scheme=BillingScheme.per_unit,
        )
        baker.make("djstripe.Product", id="prod_L4F8CtH20Oad6S", default_price=price)
        data = {"price": price.id, "organization": self.organization.id}
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.json()["price"], price.id)

        # Second attempt should fail
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 400)

    def test_create_invalid_org(self):
        """Only owners may create subscriptions"""
        user = baker.make("users.user")  # Non owner member
        plan = baker.make("djstripe.Plan", amount=0)
        self.organization.add_user(user)
        self.client.force_login(user)
        data = {"plan": plan.id, "organization": self.organization.id}
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 400)


class ProductAPITestCase(TestCase):
    def test_product_list(self):
        price = baker.make(
            "djstripe.Price",
            unit_amount=0,
            billing_scheme=BillingScheme.per_unit,
            active=True,
            product__active=True,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "true"},
        )
        inactive_price = baker.make(
            "djstripe.Price",
            unit_amount=0,
            billing_scheme=BillingScheme.per_unit,
            active=False,
            product__active=False,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "true"},
        )
        hidden_price = baker.make(
            "djstripe.Price",
            unit_amount=0,
            billing_scheme=BillingScheme.per_unit,
            active=True,
            product__active=True,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "false"},
        )
        user = baker.make("users.user")
        self.client.force_login(user)
        res = self.client.get(reverse("api:list_products"))
        self.assertContains(res, price.id)
        self.assertNotContains(res, inactive_price.id)
        self.assertNotContains(res, hidden_price.id)


# Price ID must be from a real price actually set up on Stripe Test account
class StripeAPITestCase(TestCase):
    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_create_checkout(self):
        price = baker.make(
            "djstripe.Price",
            id="price_1MZhMWJ4NuO0bv3IGMoDoFFI",
        )
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)
        url = reverse(
            "api:create_stripe_subscription_checkout", args=[organization.slug]
        )
        self.client.force_login(user)
        data = {"price": price.id}

        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_manage_billing(self):
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        url = reverse("api:stripe_billing_portal", args=[organization.slug])
        organization.add_user(user)
        self.client.force_login(user)
        data = {"organization": organization.id}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 200)
