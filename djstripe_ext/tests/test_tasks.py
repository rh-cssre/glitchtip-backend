from datetime import timedelta
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker
from freezegun import freeze_time
from glitchtip import test_utils  # pylint: disable=unused-import
from ..tasks import warn_organization_throttle


class OrganizationWarnThrottlingTestCase(TestCase):
    def test_warn_organization_throttle(self):
        user = baker.make("users.User")
        project = baker.make(
            "projects.Project", organization__owner__organization_user__user=user,
        )
        plan = baker.make(
            "djstripe.Plan", active=True, amount=0, product__metadata={"events": "10"},
        )

        project2 = baker.make(
            "projects.Project",
            organization__owner__organization_user__user=user,
            organization__djstripe_customers__subscriptions__plan=plan,
        )

        with freeze_time(timezone.datetime(2000, 1, 1)):
            subscription = baker.make(
                "djstripe.Subscription",
                customer__subscriber=project.organization,
                livemode=False,
                plan=plan,
                status="active",
            )
            subscription.current_period_end = (
                subscription.current_period_start + timedelta(days=30)
            )
            subscription.save()
            baker.make("events.Event", issue__project=project, _quantity=9)
            warn_organization_throttle()
            self.assertEqual(len(mail.outbox), 1)
            warn_organization_throttle()
            self.assertEqual(len(mail.outbox), 1)

        with freeze_time(timezone.datetime(2000, 2, 2)):
            subscription.current_period_start = timezone.make_aware(
                timezone.datetime(2000, 2, 1)
            )
            subscription.current_period_end = (
                subscription.current_period_start + timedelta(days=30)
            )
            subscription.save()
            warn_organization_throttle()
            self.assertEqual(len(mail.outbox), 1)

            baker.make("events.Event", issue__project=project, _quantity=9)
            warn_organization_throttle()
            self.assertEqual(len(mail.outbox), 2)
