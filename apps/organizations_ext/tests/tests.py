from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import EventCounts, OrganizationOwner


class EventCountsTestCase(SimpleTestCase):
    def test_weighted_total(self):
        """Errors, transactions and file-size MB weigh 1.0; logs and uptime weigh 0.1."""
        self.assertEqual(EventCounts(issue_event_count=100).total_event_count, 100)
        self.assertEqual(EventCounts(transaction_count=100).total_event_count, 100)
        self.assertEqual(EventCounts(file_size=100).total_event_count, 100)
        # 10 logs / 10 uptime checks each count as 1 event
        self.assertEqual(EventCounts(log_count=100).total_event_count, 10)
        self.assertEqual(
            EventCounts(uptime_check_event_count=100).total_event_count, 10
        )
        # Sub-10 batches floor to 0 (integer //10)
        self.assertEqual(EventCounts(uptime_check_event_count=9).total_event_count, 0)
        # Mixed: 50 errors + 30 logs + 40 uptime = 50 + 3 + 4
        self.assertEqual(
            EventCounts(
                issue_event_count=50, log_count=30, uptime_check_event_count=40
            ).total_event_count,
            57,
        )


class OrganizationModelTestCase(TestCase):
    def test_email(self):
        """Billing email address"""
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)

        # Org 1 has two users and only one of which is an owner
        user2 = baker.make("users.user")
        organization2 = baker.make("organizations_ext.Organization")
        organization2.add_user(user2)
        organization.add_user(user2)

        self.assertEqual(organization.email, user.email)
        self.assertEqual(organization.users.count(), 2)
        self.assertEqual(organization.owners.count(), 1)

    def test_email_missing_organization_owner_fallback(self):
        """
        When OrganizationOwner record is missing, email property should
        fall back to first user with OWNER role and log a warning.
        """
        user = baker.make("users.user")
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(user)

        # Verify the owner exists first
        self.assertTrue(
            OrganizationOwner.objects.filter(organization=organization).exists()
        )
        self.assertEqual(organization.email, user.email)

        # Delete the OrganizationOwner to simulate the data integrity issue
        OrganizationOwner.objects.filter(organization=organization).delete()

        # Refresh from DB to clear cached relation
        organization.refresh_from_db()

        # Verify fallback works and warning is logged
        with mock.patch("apps.organizations_ext.models.logger") as mock_logger:
            email = organization.email
            self.assertEqual(email, user.email)
            mock_logger.warning.assert_called_once()
            self.assertIn("no OrganizationOwner", mock_logger.warning.call_args[0][0])

    def test_email_no_owner_at_all(self):
        """
        When organization has no OrganizationOwner and no users with OWNER role,
        email property should return None.
        """
        organization = baker.make("organizations_ext.Organization")

        # Add a user but not as owner
        user = baker.make("users.user")
        baker.make(
            "organizations_ext.OrganizationUser",
            user=user,
            organization=organization,
            role=OrganizationUserRole.MEMBER,
        )

        with mock.patch("apps.organizations_ext.models.logger"):
            self.assertIsNone(organization.email)

    def test_slug_reserved_words(self):
        """Reserve some words for frontend routing needs"""
        word = "login"
        organization = baker.make("organizations_ext.Organization", name=word)
        self.assertNotEqual(organization.slug, word)
        organization = baker.make("organizations_ext.Organization", name=word)


class OrganizationRegistrationSettingQueryTestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = reverse("api:list_organizations")

    @override_settings(ENABLE_ORGANIZATION_CREATION=False)
    def test_organizations_closed_registration_first_organization_create(self):
        data = {"name": "test"}
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)


class OrganizationsFilterTestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.url = reverse("api:list_organizations")

    def test_default_ordering(self):
        organizationA = baker.make(
            "organizations_ext.Organization", name="A Organization"
        )
        organizationZ = baker.make(
            "organizations_ext.Organization", name="Z Organization"
        )
        organizationB = baker.make(
            "organizations_ext.Organization", name="B Organization"
        )
        organizationA.add_user(self.user)
        organizationB.add_user(self.user)
        organizationZ.add_user(self.user)
        res = self.client.get(self.url)
        data = res.json()
        self.assertEqual(data[0]["name"], organizationA.name)
        self.assertEqual(data[2]["name"], organizationZ.name)
