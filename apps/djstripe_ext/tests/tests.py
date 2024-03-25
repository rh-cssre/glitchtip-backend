from unittest import skipIf

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from djstripe.enums import BillingScheme
from freezegun import freeze_time
from model_bakery import baker
from rest_framework.test import APITestCase


class SubscriptionAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.client.force_login(self.user)
        self.url = reverse("subscription-list")

    def test_list(self):
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        subscription = baker.make(
            "djstripe.Subscription", customer=customer, livemode=False
        )

        subscription2 = baker.make("djstripe.Subscription", livemode=False)
        subscription3 = baker.make(
            "djstripe.Subscription", customer=customer, livemode=True
        )

        res = self.client.get(self.url)
        self.assertContains(res, subscription.id)
        self.assertNotContains(res, subscription2.id)
        self.assertNotContains(res, subscription3.id)

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
        url = reverse("subscription-detail", args=[self.organization.slug])
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
        url = (
            reverse("subscription-detail", args=[self.organization.slug])
            + "events_count/"
        )
        with freeze_time(timezone.datetime(2020, 3, 1)):
            baker.make("events.Event", issue__project__organization=self.organization)
        with freeze_time(timezone.datetime(2020, 1, 5)):
            baker.make("events.Event")
            baker.make("events.Event", issue__project__organization=self.organization)
            baker.make(
                "performance.TransactionEvent",
                group__project__organization=self.organization,
            )
            baker.make(
                "releases.ReleaseFile",
                file__blob__size=1000000,
                release__organization=self.organization,
                _quantity=2,
            )
        res = self.client.get(url)
        self.assertEqual(
            res.data,
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
        url = (
            reverse("subscription-detail", args=[self.organization.slug])
            + "events_count/"
        )
        res = self.client.get(url)
        self.assertEqual(sum(res.data.values()), 0)

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
        res = self.client.post(self.url, data)
        self.assertEqual(res.data["price"], price.id)

        # Second attempt should fail
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 409)

    def test_create_invalid_org(self):
        """Only owners may create subscriptions"""
        user = baker.make("users.user")  # Non owner member
        plan = baker.make("djstripe.Plan", amount=0)
        self.organization.add_user(user)
        self.client.force_login(user)
        data = {"plan": plan.id, "organization": self.organization.id}
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 400)


class ProductAPITestCase(APITestCase):
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
        res = self.client.get(reverse("product-list"))
        self.assertContains(res, price.id)
        self.assertNotContains(res, inactive_price.id)
        self.assertNotContains(res, hidden_price.id)


# Price ID must be from a real price actually set up on Stripe Test account
class StripeAPITestCase(APITestCase):
    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_create_checkout(self):
        url = reverse("create-stripe-subscription-checkout")
        price = baker.make(
            "djstripe.Price",
            id="price_1MZhMWJ4NuO0bv3IGMoDoFFI",
        )
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)
        self.client.force_login(user)
        data = {"price": price.id, "organization": organization.id}

        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 200)

    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_manage_billing(self):
        url = reverse("create-billing-portal")
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)
        self.client.force_login(user)
        data = {"organization": organization.id}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 200)


class SubscriptionIntegrationAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        # Make these in this manner to avoid syncing data to stripe actual
        self.price = baker.make(
            "djstripe.Price",
            active=True,
            unit_amount=0,
            billing_scheme=BillingScheme.per_unit,
        )
        self.customer = baker.make(
            "djstripe.Customer", subscriber=self.organization, livemode=False
        )
        self.client.force_login(self.user)
        self.list_url = reverse("subscription-list")
        self.detail_url = reverse("subscription-detail", args=[self.organization.slug])
