from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.issue_events.models import Issue, IssueEvent
from apps.logs.models import LogEvent
from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription
from apps.uptime.models import MonitorCheck
from glitchtip.partition_manager import UUID7Helper

from ..tasks import delete_organization

_delete_organization_sync = async_to_sync(delete_organization.func)


class DeleteOrganizationTaskTestCase(TestCase):
    """Verify delete_organization batch-deletes partitioned data before cascade."""

    def test_delete_org_with_issue_events(self):
        """IssueEvents in partitioned tables must be deleted."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        issue = baker.make("issue_events.Issue", project=project)
        event = IssueEvent.objects.create(
            issue=issue,
            organization=org,
            timestamp=timezone.now(),
            type=0,
            level=4,
            title="Test",
            data={},
            tags={},
        )

        _delete_organization_sync(org.id)

        self.assertFalse(Organization.objects.filter(id=org.id).exists())
        self.assertFalse(IssueEvent.objects.filter(id=event.id).exists())
        self.assertFalse(Issue.objects.filter(id=issue.id).exists())

    def test_delete_org_with_log_events(self):
        """LogEvents in partitioned tables must be deleted."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(timezone.now()),
            organization=org,
            project=project,
            level=4,
            body="test log",
            data={},
        )

        _delete_organization_sync(org.id)

        self.assertFalse(Organization.objects.filter(id=org.id).exists())
        self.assertFalse(LogEvent.objects.filter(id=log.id).exists())

    def test_delete_org_with_monitor_checks(self):
        """MonitorChecks in partitioned tables must be deleted."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        monitor = baker.make("uptime.Monitor", project=project, organization=org)
        check = baker.make(MonitorCheck, monitor=monitor, organization=org)

        _delete_organization_sync(org.id)

        self.assertFalse(Organization.objects.filter(id=org.id).exists())
        self.assertFalse(MonitorCheck.objects.filter(id=check.id).exists())

    @override_settings(BILLING_ENABLED=True)
    def test_delete_org_cancels_stripe_subscription(self):
        """Deleting an org must cancel its active subscription in Stripe."""
        now = timezone.now()
        org = baker.make("organizations_ext.Organization")
        product = baker.make("stripe.StripeProduct", events=1000)
        price = baker.make("stripe.StripePrice", product=product)
        subscription = StripeSubscription.objects.create(
            stripe_id="sub_active",
            created=now,
            current_period_start=now,
            current_period_end=now,
            start_date=now,
            price=price,
            organization=org,
            status=SubscriptionStatus.ACTIVE,
        )

        with patch(
            "apps.stripe.models.cancel_subscription", new_callable=AsyncMock
        ) as mock_cancel:
            _delete_organization_sync(org.id)

        mock_cancel.assert_awaited_once_with(subscription.stripe_id)
        self.assertFalse(Organization.objects.filter(id=org.id).exists())

    def test_delete_org_via_api(self):
        """Full round-trip: API soft-deletes, task batch-deletes partitioned data."""
        from django.urls import reverse

        user = baker.make("users.user")
        org = baker.make("organizations_ext.Organization")
        org.add_user(user, OrganizationUserRole.OWNER)
        project = baker.make("projects.Project", organization=org)
        issue = baker.make("issue_events.Issue", project=project)
        event = IssueEvent.objects.create(
            issue=issue,
            organization=org,
            timestamp=timezone.now(),
            type=0,
            level=4,
            title="Test",
            data={},
            tags={},
        )

        self.client.force_login(user)
        url = reverse("api:delete_organization", args=[org.slug])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)

        # With immediate task backend, everything is fully deleted
        self.assertFalse(Organization.objects.filter(id=org.id).exists())
        self.assertFalse(IssueEvent.objects.filter(id=event.id).exists())
        self.assertFalse(Issue.objects.filter(id=issue.id).exists())
