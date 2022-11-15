from unittest.mock import patch
from unittest import skipIf
from django.shortcuts import reverse
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APITestCase
from model_bakery import baker
from freezegun import freeze_time
from glitchtip import test_utils  # pylint: disable=unused-import


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
            customer=customer,
            livemode=False,
            created=timezone.make_aware(timezone.datetime(2020, 1, 2)),
        )
        # Should only get most recent
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            created=timezone.make_aware(timezone.datetime(2020, 1, 1)),
        )
        baker.make("djstripe.Subscription")
        url = reverse("subscription-detail", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, subscription.id)

    def test_events_count(self):
        customer = baker.make("djstripe.Customer", subscriber=self.organization)
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            current_period_start=timezone.make_aware(timezone.datetime(2020, 1, 2)),
            current_period_end=timezone.make_aware(timezone.datetime(2020, 2, 2)),
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

    @patch("djstripe.models.Customer.subscribe")
    def test_create(self, djstripe_customer_subscribe_mock):
        customer = baker.make(
            "djstripe.Customer", subscriber=self.organization, livemode=False
        )
        plan = baker.make("djstripe.Plan", amount=0)
        subscription = baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
        )
        djstripe_customer_subscribe_mock.return_value = subscription
        data = {"plan": plan.id, "organization": self.organization.id}
        res = self.client.post(self.url, data)
        self.assertEqual(res.data["plan"], plan.id)

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
        plan = baker.make(
            "djstripe.Plan",
            amount=0,
            livemode=False,
            active=True,
            product__active=True,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "true"},
        )
        inactive_plan = baker.make(
            "djstripe.Plan",
            amount=0,
            livemode=False,
            active=False,
            product__active=False,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "true"},
        )
        hidden_plan = baker.make(
            "djstripe.Plan",
            amount=0,
            livemode=False,
            active=True,
            product__active=True,
            product__livemode=False,
            product__metadata={"events": 10, "is_public": "false"},
        )
        user = baker.make("users.user")
        self.client.force_login(user)
        res = self.client.get(reverse("product-list"))
        self.assertContains(res, plan.id)
        self.assertNotContains(res, inactive_plan.id)
        self.assertNotContains(res, hidden_plan.id)


class StripeAPITestCase(APITestCase):
    @skipIf(
        settings.STRIPE_TEST_PUBLIC_KEY == "fake", "requires real Stripe test API key"
    )
    def test_create_checkout(self):
        url = reverse("create-stripe-subscription-checkout")
        plan = baker.make(
            "djstripe.Plan",
            amount=1,
            livemode=False,
            active=True,
            id="price_HNfVNr3ohLWkmv",
            description="Small - 100k events",
            product__active=True,
            product__livemode=False,
        )
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)
        self.client.force_login(user)
        data = {"plan": plan.id, "organization": organization.id}

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
        self.plan = baker.make("djstripe.Plan", active=True, amount=0)
        self.customer = baker.make(
            "djstripe.Customer", subscriber=self.organization, livemode=False
        )
        self.client.force_login(self.user)
        self.list_url = reverse("subscription-list")
        self.detail_url = reverse("subscription-detail", args=[self.organization.slug])

    @patch("djstripe.models.Customer.subscribe")
    def test_new_org_flow(self, djstripe_customer_subscribe_mock):
        """Test checking if subscription exists and when not, creating a free tier one"""
        res = self.client.get(self.detail_url)
        self.assertFalse(res.data["id"])  # No subscription, user should create one

        subscription = baker.make(
            "djstripe.Subscription",
            customer=self.customer,
            livemode=False,
        )
        djstripe_customer_subscribe_mock.return_value = subscription

        data = {"plan": self.plan.id, "organization": self.organization.id}
        res = self.client.post(self.list_url, data)
        self.assertContains(res, self.plan.id, status_code=201)
        djstripe_customer_subscribe_mock.assert_called_once()

        res = self.client.get(self.detail_url)
        self.assertEqual(res.data["id"], subscription.id)
