from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.email import ThrottleNoticeEmail
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription


@override_settings(EMAIL_ENABLED=True)
class ThrottleNoticeEmailTestCase(TestCase):
    """A paid org cannot change plan itself yet, so the notice sends it to
    support instead of the free org's upgrade link, and an org at its
    on-demand spend cap must not be told it merely exceeded its plan."""

    def setUp(self):
        self.org = baker.make("organizations_ext.Organization", event_throttle_rate=10)

    def subscribe(self, price):
        product = baker.make("stripe.StripeProduct", events=100_000)
        now = timezone.now()
        sub = StripeSubscription.objects.create(
            stripe_id=f"sub_{price}",
            created=now,
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            start_date=now,
            price=baker.make("stripe.StripePrice", product=product, price=price),
            organization=self.org,
            status=SubscriptionStatus.ACTIVE,
        )
        self.org.stripe_primary_subscription = sub
        self.org.save(update_fields=["stripe_primary_subscription"])
        return sub

    def provision_on_demand(self):
        product = baker.make("stripe.StripeProduct", events=None, is_overage=True)
        baker.make("stripe.StripePrice", product=product, price=0, is_metered=True)

    def send(self):
        ThrottleNoticeEmail(pk=self.org.pk, total_event_count=120_000).send_email(
            "billing@example.com"
        )
        message = mail.outbox[-1]
        return message.subject, message.body, message.alternatives[0][0]

    def test_free_org_is_told_to_upgrade(self):
        self.subscribe(price=0)
        subject, text, html = self.send()
        self.assertIn("Met event quota", subject)
        for body in (text, html):
            self.assertIn("Upgrade", body)
            self.assertNotIn("contact us", body)
            self.assertNotIn("on-demand", body)

    def test_paid_org_is_offered_on_demand_and_a_plan_change(self):
        self.subscribe(price=15)
        self.provision_on_demand()
        subject, text, html = self.send()
        self.assertIn("Met event quota", subject)
        for body in (text, html):
            self.assertIn("Turn on on-demand billing", body)
            self.assertIn("support@glitchtip.com", body)
            self.assertIn("billing is turned on or you upgrade your plan", body)
            self.assertNotIn("Upgrade", body)

    def test_paid_org_is_not_offered_on_demand_the_server_lacks(self):
        self.subscribe(price=15)
        _, text, html = self.send()
        for body in (text, html):
            self.assertNotIn("on-demand", body)
            self.assertIn("support@glitchtip.com", body)
            self.assertIn("It lifts once you upgrade your plan", body)

    def test_org_at_its_spend_cap_is_told_so(self):
        sub = self.subscribe(price=15)
        sub.metered_item_id = "si_1"
        sub.save(update_fields=["metered_item_id"])
        self.org.metered_billing_enabled = True
        self.org.save(update_fields=["metered_billing_enabled"])
        self.provision_on_demand()
        subject, text, html = self.send()
        self.assertIn("Reached the on-demand spend cap", subject)
        for body in (text, html):
            self.assertIn("reached its on-demand billing spend cap", body)
            self.assertIn("no further on-demand usage is charged", body)
            self.assertIn("Raise your spend cap", body)
            self.assertIn("raised above your usage or you upgrade your plan", body)
            self.assertNotIn("exceeded its monthly event limit", body)
            self.assertNotIn("Turn on on-demand billing", body)
            self.assertNotIn("Upgrade", body)
