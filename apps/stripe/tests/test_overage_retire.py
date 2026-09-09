import json
from unittest.mock import AsyncMock, MagicMock, patch

from dateutil.relativedelta import relativedelta
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.schema import Subscription
from apps.stripe.tests.webhook_helpers import StripeWebhookMixin
from apps.stripe.views import stripe_webhook_view

CUSTOMER = {
    "object": "customer",
    "id": "cus_test",
    "email": None,
    "name": None,
    "metadata": {},
}


@override_settings(
    STRIPE_WEBHOOK_SECRET="test_webhook_secret",
    STRIPE_WEBHOOK_TOLERANCE=300,
    STRIPE_REGION="",
)
class RetireOverageTestCase(StripeWebhookMixin, TestCase):
    """Overage state must die with the subscription that carried it, on the
    paths test_views.py's cancel tests skip: the pointer has already moved, or a
    sweep noticed the death instead of a webhook.
    """

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.start_ts = int(now.timestamp())
        self.end_ts = int((now + relativedelta(months=1)).timestamp())

        self.org = Organization.objects.create(
            name="Test Org", metered_billing_enabled=True, stripe_customer_id="cus_test"
        )
        self.product = StripeProduct.objects.create(
            stripe_id="prod_plan", name="Plan", events=1000, is_public=True
        )
        self.price = StripePrice.objects.create(
            stripe_id="price_plan",
            product=self.product,
            price=10,
            nickname="Monthly",
            interval="month",
        )
        # Older than any resubscribe, so get_primary_subscription's "-created"
        # tie-break hands the pointer to the replacement — the ordering that
        # makes the race below reachable.
        self.org.stripe_primary_subscription = self._subscription(
            "sub_old",
            metered_item_id="si_sub_old_over",
            created=now - relativedelta(months=6),
        )
        self.org.save(update_fields=["stripe_primary_subscription"])

    def _subscription(self, stripe_id, *, metered_item_id="", created=None):
        now = timezone.now()
        return StripeSubscription.objects.create(
            stripe_id=stripe_id,
            created=created or now,
            current_period_start=now,
            current_period_end=now + relativedelta(months=1),
            subscription_cycle_start=now,
            subscription_cycle_end=now + relativedelta(months=1),
            price=self.price,
            organization=self.org,
            status=SubscriptionStatus.ACTIVE,
            start_date=created or now,
            metered_item_id=metered_item_id,
        )

    def _event(self, sub_id, status, *, overage_item=False):
        return self.generate_subscription_event_data(
            type="customer.subscription.updated",
            event_id=f"evt_{sub_id}_{status}",
            subscription_id=sub_id,
            current_period_start=self.start_ts,
            current_period_end=self.end_ts,
            price_id=self.price.stripe_id,
            product_id=self.product.stripe_id,
            status=status,
            metered_item_id=f"si_{sub_id}_over" if overage_item else None,
        )

    async def _deliver(self, sub_id, status, *, overage_item=False, throttle=None):
        request = self.generate_stripe_request(
            self._event(sub_id, status, overage_item=overage_item)
        )
        customer = {**CUSTOMER, "metadata": {"organization_id": str(self.org.id)}}
        with patch("apps.stripe.views.stripe_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = json.dumps(customer)
            if throttle is None:
                return await stripe_webhook_view(request)
            with patch("apps.stripe.views.check_organization_throttle", new=throttle):
                return await stripe_webhook_view(request)

    async def _org(self):
        return await Organization.objects.aget(id=self.org.id)

    async def _item_id(self, stripe_id):
        sub = await StripeSubscription.objects.aget(stripe_id=stripe_id)
        return sub.metered_item_id

    async def _add_replacement(self):
        """A resubscribe that claims the primary pointer, carrying no item."""
        now = timezone.now()
        await StripeSubscription.objects.acreate(
            stripe_id="sub_new",
            created=now,
            current_period_start=now,
            current_period_end=now + relativedelta(months=1),
            subscription_cycle_start=now,
            subscription_cycle_end=now + relativedelta(months=1),
            price_id=self.price.stripe_id,
            organization_id=self.org.id,
            status=SubscriptionStatus.ACTIVE,
            start_date=now,
        )
        await StripeSubscription.set_primary_subscriptions_for_organizations(
            {self.org.id}
        )

    async def test_cancel_arriving_after_the_resub_still_retires(self):
        """The regression. Stripe does not order deliveries, so the resub can
        claim the primary pointer first, and the cleanup used to be skipped
        outright because it only ran for the subscription the pointer named."""
        await self._deliver("sub_new", "active")
        throttle = MagicMock(aenqueue=AsyncMock())
        await self._deliver("sub_old", "canceled", overage_item=True, throttle=throttle)

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")
        # Neither pointer branch ran, so the retire has to ask for the re-check.
        throttle.aenqueue.assert_awaited_once_with(self.org.id, bypass_cache=True)

    async def test_cancel_retires_when_the_payload_drops_the_item(self):
        """Stripe need not still list the item on a canceled subscription, so
        the row's pre-upsert value is what says the subscription carried one."""
        await self._deliver("sub_new", "active")
        await self._deliver("sub_old", "canceled")

        org = await self._org()
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_replacement_carrying_its_own_item_keeps_the_opt_in(self):
        """The customer re-enabled overage on the new subscription; retiring the
        old one must not switch that back off."""
        await self._deliver("sub_new", "active", overage_item=True)
        await self._deliver("sub_old", "canceled", overage_item=True)

        org = await self._org()
        self.assertTrue(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")
        self.assertEqual(await self._item_id("sub_new"), "si_sub_new_over")

    async def test_item_vanishing_from_a_live_subscription_keeps_the_opt_in(self):
        """Not a terminal transition, so nothing is retired: the org stays
        enabled-but-inactive, which the API reports as our fault, not theirs."""
        await self._deliver("sub_old", "active")

        org = await self._org()
        self.assertTrue(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_update_outdated_subscriptions_retires_what_it_cancels(self):
        """Canceled subscriptions drop out of Stripe's listing, so this sweep is
        the only path that reconciles a cancel whose webhook was lost. It writes
        the terminal status and blanks the item in one save, which leaves no
        evidence the sub carried overage unless the old id is carried across."""
        await StripeSubscription.objects.filter(stripe_id="sub_old").aupdate(
            current_period_end=timezone.now() - relativedelta(days=10)
        )
        await self._add_replacement()
        fetched = Subscription.model_validate(
            self._event("sub_old", SubscriptionStatus.CANCELED)["data"]["object"]
        )
        with patch(
            "apps.stripe.models.fetch_subscription",
            new=AsyncMock(return_value=fetched),
        ):
            await StripeSubscription.update_outdated_subscriptions()

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_sweep_retires_when_no_cancel_webhook_ever_arrived(self):
        """sync_from_stripe's tail, run in the order that used to strand the
        flag: the pointer has already moved off the terminal subscription."""
        await StripeSubscription.objects.filter(stripe_id="sub_old").aupdate(
            status=SubscriptionStatus.CANCELED
        )
        await self._add_replacement()
        await StripeSubscription.remove_inactive_primary_subscriptions()
        await StripeSubscription.retire_dead_overage_items()

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")
