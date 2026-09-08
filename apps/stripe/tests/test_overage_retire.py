import hmac
import json
import time
from unittest.mock import AsyncMock, patch

from dateutil.relativedelta import relativedelta
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.views import stripe_webhook_view

WEBHOOK_SECRET = "test_webhook_secret"


@override_settings(STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET, STRIPE_REGION="")
class RetireOverageTestCase(TestCase):
    """Overage state must die with the subscription that carried it.

    The org starts on ``sub_old`` (paid, active, overage on, metered item
    attached) and each test drives real webhook deliveries from there.
    """

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.url = reverse("stripe_webhook")
        now = timezone.now()
        self.start_ts = int(now.timestamp())
        self.end_ts = int((now + relativedelta(months=1)).timestamp())

        self.org = Organization.objects.create(
            name="Test Org",
            metered_billing_enabled=True,
            stripe_customer_id="cus_test",
        )
        product = StripeProduct.objects.create(
            stripe_id="prod_plan", name="Plan", events=1000, is_public=True
        )
        self.price = StripePrice.objects.create(
            stripe_id="price_plan",
            product=product,
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

    def _subscription(
        self, stripe_id, *, status=None, metered_item_id="", created=None
    ):
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
            status=status or SubscriptionStatus.ACTIVE,
            start_date=now,
            metered_item_id=metered_item_id,
        )

    def _item(self, sub_id, *, metered):
        recurring = {"interval": "month"}
        if metered:
            recurring |= {"usage_type": "metered", "meter": "mtr_test"}
        return {
            "id": f"si_{sub_id}_over" if metered else f"si_{sub_id}",
            "object": "subscription_item",
            "price": {
                "id": "price_over" if metered else self.price.stripe_id,
                "product": "prod_over" if metered else "prod_plan",
                "recurring": recurring,
                "object": "price",
                "active": True,
                "created": self.start_ts,
                "currency": "usd",
                "livemode": False,
                "type": "recurring",
                "billing_scheme": "per_unit",
                "lookup_key": None,
                "nickname": None,
                "tax_behavior": "unspecified",
                "tiers_mode": None,
                "unit_amount": None if metered else 1000,
                "unit_amount_decimal": None if metered else "1000",
                "metadata": {},
            },
            "quantity": 1,
            "subscription": sub_id,
            "tax_rates": [],
            "metadata": {},
            "created": self.start_ts,
            "current_period_start": self.start_ts,
            "current_period_end": self.end_ts,
        }

    async def _deliver(self, sub_id, status, *, overage_item=False):
        items = [self._item(sub_id, metered=False)]
        if overage_item:
            items.append(self._item(sub_id, metered=True))
        payload = {
            "type": "customer.subscription.updated",
            "id": f"evt_{sub_id}_{status}",
            "data": {
                "object": {
                    "object": "subscription",
                    "id": sub_id,
                    "customer": "cus_test",
                    "items": {"object": "list", "data": items},
                    "status": status,
                    "created": self.start_ts,
                    "start_date": self.start_ts,
                    "collection_method": "charge_automatically",
                    "current_period_start": self.start_ts,
                    "current_period_end": self.end_ts,
                    "metadata": {},
                    "livemode": False,
                    "cancel_at_period_end": False,
                }
            },
            "api_version": "",
            "created": self.start_ts,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idempotency_key": f"key_{sub_id}_{status}"},
        }
        body = json.dumps(payload).encode()
        timestamp = int(time.time())
        signature = hmac.new(
            WEBHOOK_SECRET.encode(),
            f"{timestamp}.{body.decode()}".encode(),
            digestmod="sha256",
        ).hexdigest()
        request = self.factory.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={signature}",
        )
        customer = {
            "object": "customer",
            "id": "cus_test",
            "metadata": {"organization_id": str(self.org.id)},
            "email": None,
            "name": None,
        }
        with patch("apps.stripe.views.stripe_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = json.dumps(customer)
            await stripe_webhook_view(request)

    async def _org(self):
        return await Organization.objects.aget(id=self.org.id)

    async def _item_id(self, stripe_id):
        sub = await StripeSubscription.objects.aget(stripe_id=stripe_id)
        return sub.metered_item_id

    async def test_cancel_arriving_after_the_resub_still_retires(self):
        """The regression. Stripe does not order deliveries, so the resub can
        claim the primary pointer first, and the cleanup used to be skipped
        outright because it only ran for the subscription the pointer named."""
        await self._deliver("sub_new", "active")
        await self._deliver("sub_old", "canceled", overage_item=True)

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_cancel_arriving_before_the_resub_still_retires(self):
        await self._deliver("sub_old", "canceled", overage_item=True)
        await self._deliver("sub_new", "active")

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_cancel_retires_when_the_payload_drops_the_item(self):
        """Stripe need not still list the item on a canceled subscription, so
        the row's pre-upsert value is what says the subscription carried one."""
        await self._deliver("sub_new", "active")
        await self._deliver("sub_old", "canceled")

        org = await self._org()
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")

    async def test_recoverable_status_keeps_the_opt_in(self):
        """unpaid recovers onto this same row with its item intact."""
        await self._deliver("sub_old", "unpaid", overage_item=True)

        org = await self._org()
        self.assertTrue(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "si_sub_old_over")

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

    async def test_sweep_retires_when_no_cancel_webhook_ever_arrived(self):
        """sync_from_stripe's tail, run in the order that used to strand the
        flag: the pointer has already moved off the terminal subscription."""
        await StripeSubscription.objects.filter(stripe_id="sub_old").aupdate(
            status=SubscriptionStatus.CANCELED
        )
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
        await StripeSubscription.remove_inactive_primary_subscriptions()
        await StripeSubscription.retire_dead_overage_items()

        org = await self._org()
        self.assertEqual(org.stripe_primary_subscription_id, "sub_new")
        self.assertFalse(org.metered_billing_enabled)
        self.assertEqual(await self._item_id("sub_old"), "")
