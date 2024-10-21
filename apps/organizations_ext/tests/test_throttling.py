from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from ..tasks import (
    get_free_tier_organizations_with_event_count,
    set_organization_throttle,
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
                current_period_end=timezone.make_aware(timezone.datetime(2000, 1, 31)),
            )
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=organization,
                _quantity=3,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=organization,
                count=3,
            )
            set_organization_throttle()
            organization.refresh_from_db()
            self.assertTrue(organization.is_accepting_events)

            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=organization,
                _quantity=8,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=organization,
                count=8,
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
                "issue_events.IssueEvent",
                issue__project__organization=organization,
                _quantity=10,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=organization,
                count=10,
            )
            baker.make(
                "performance.TransactionEvent",
                group__project__organization=organization,
                _quantity=1,
            )
            baker.make(
                "projects.TransactionEventProjectHourlyStatistic",
                project__organization=organization,
                count=1,
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
                current_period_end=timezone.make_aware(timezone.datetime(2000, 2, 1)),
            )
            baker.make("issue_events.IssueEvent", issue__project=project, _quantity=3)
            baker.make(
                "projects.IssueEventProjectHourlyStatistic", project=project, count=3
            )
            baker.make(
                "performance.TransactionEvent",
                group__project=project,
                _quantity=2,
            )
            baker.make(
                "projects.TransactionEventProjectHourlyStatistic",
                project=project,
                count=2,
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
                "issue_events.IssueEvent",
                issue__project__organization=organization,
                _quantity=2,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=organization,
                count=2,
            )
        with self.assertNumQueries(4):
            set_organization_throttle()

    @override_settings(BILLING_FREE_TIER_EVENTS=1)
    def test_no_plan_throttle(self):
        """
        It's possible to not sign up for a free plan, they should be limited to free tier events
        """
        organization = baker.make("organizations_ext.Organization")
        user = baker.make("users.user")
        organization.add_user(user)
        project = baker.make("projects.Project", organization=organization)
        baker.make("issue_events.IssueEvent", issue__project=project, _quantity=2)
        set_organization_throttle()
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Make plan active
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )
        plan = baker.make("djstripe.Plan", active=True, amount=1)
        subscription = baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=plan,
            status="active",
            current_period_end=timezone.make_aware(timezone.datetime(2000, 1, 31)),
        )
        set_organization_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)

        # Cancel plan
        subscription.status = "canceled"
        subscription.save()
        set_organization_throttle()
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Add new active plan (still has canceled plan)
        subscription = baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=plan,
            status="active",
            current_period_end=timezone.make_aware(timezone.datetime(2000, 1, 31)),
        )
        set_organization_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)

    def test_canceled_plan(self):
        # Start with no plan and throttled
        organization = baker.make(
            "organizations_ext.Organization", is_accepting_events=False
        )
        user = baker.make("users.user")
        organization.add_user(user)
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Add old paid plan and active free plan
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )
        free_plan = baker.make("djstripe.Plan", active=True, amount=0)
        paid_plan = baker.make("djstripe.Plan", active=True, amount=1)
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=paid_plan,
            status="canceled",
            current_period_end=timezone.make_aware(timezone.datetime(2000, 1, 31)),
        )
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=free_plan,
            status="active",
            current_period_end=timezone.make_aware(timezone.datetime(2100, 1, 31)),
        )

        # Should not be throttled
        set_organization_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)
