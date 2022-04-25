from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker
from freezegun import freeze_time
from glitchtip import test_utils  # pylint: disable=unused-import
from ..tasks import (
    set_organization_throttle,
    get_free_tier_organizations_with_event_count,
)


class OrganizationThrottlingTestCase(TestCase):
    @override_settings(BILLING_FREE_TIER_EVENTS=10)
    def test_non_subscriber_throttling(self):
        plan = baker.make("djstripe.Plan", active=True, amount=0)
        organization = baker.make("organizations_ext.Organization")
        user = baker.make("users.user")
        organization.add_user(user)
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )

        with freeze_time(timezone.datetime(2000, 1, 1)):
            subscription = baker.make(
                "djstripe.Subscription",
                customer=customer,
                livemode=False,
                plan=plan,
                status="active",
                current_period_end="2000-01-31",
            )
            baker.make(
                "events.Event", issue__project__organization=organization, _quantity=3
            )
            set_organization_throttle()
            organization.refresh_from_db()
            self.assertTrue(organization.is_accepting_events)

            baker.make(
                "events.Event", issue__project__organization=organization, _quantity=8
            )
            set_organization_throttle()
            organization.refresh_from_db()
            self.assertFalse(organization.is_accepting_events)
            self.assertTrue(mail.outbox[0])

        with freeze_time(timezone.datetime(2000, 2, 1)):
            # Month should reset throttle
            subscription.current_period_start = timezone.make_aware(
                timezone.datetime(2000, 2, 1)
            )
            subscription.current_period_end = timezone.make_aware(
                timezone.datetime(2000, 2, 28)
            )
            subscription.save()
            set_organization_throttle()
            organization.refresh_from_db()
            self.assertTrue(organization.is_accepting_events)

            # Throttle again
            baker.make(
                "events.Event", issue__project__organization=organization, _quantity=10
            )
            baker.make(
                "performance.TransactionEvent",
                group__project__organization=organization,
                _quantity=1,
            )
            set_organization_throttle()
            organization.refresh_from_db()
            self.assertFalse(organization.is_accepting_events)

    def test_organization_event_count(self):
        plan = baker.make("djstripe.Plan", active=True, amount=0)
        organization = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=organization)
        user = baker.make("users.user")
        organization.add_user(user)
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )

        with freeze_time(timezone.datetime(2000, 1, 1)):
            baker.make(
                "djstripe.Subscription",
                customer=customer,
                livemode=False,
                plan=plan,
                status="active",
                current_period_end="2000-02-01",
            )
            baker.make("events.Event", issue__project=project, _quantity=3)
            baker.make(
                "performance.TransactionEvent",
                group__project=project,
                _quantity=2,
            )
            free_org = get_free_tier_organizations_with_event_count().first()
        self.assertEqual(free_org.total_event_count, 5)

    @override_settings(BILLING_FREE_TIER_EVENTS=1)
    def test_non_subscriber_throttling_performance(self):
        for _ in range(2):
            plan = baker.make("djstripe.Plan", active=True, amount=0)
            organization = baker.make("organizations_ext.Organization")
            user = baker.make("users.user")
            organization.add_user(user)
            customer = baker.make(
                "djstripe.Customer", subscriber=organization, livemode=False
            )
            baker.make(
                "djstripe.Subscription",
                customer=customer,
                livemode=False,
                plan=plan,
                status="active",
            )
            baker.make(
                "events.Event", issue__project__organization=organization, _quantity=2
            )
        with self.assertNumQueries(4):
            set_organization_throttle()
