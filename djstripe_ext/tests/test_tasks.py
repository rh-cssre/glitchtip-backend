from django.core import mail
from django.test import TestCase, override_settings
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
            "djstripe.Plan",
            active=True,
            amount=0,
            product__metadata={"events": "10"},
            subscriptions__customer__subscriber=project.organization,
            subscriptions__status="active",
        )

        project2 = baker.make(
            "projects.Project",
            organization__owner__organization_user__user=user,
            organization__djstripe_customers__subscriptions__plan=plan,
        )

        baker.make("events.Event", issue__project=project, _quantity=9)
        warn_organization_throttle()
        self.assertEqual(len(mail.outbox), 1)
