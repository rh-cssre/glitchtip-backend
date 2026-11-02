from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from model_bakery import baker

from apps.users.models import User

from ..constants import OrganizationUserRole
from ..models import Organization, OrganizationOwner


class OrganizationMemberGuardTestCase(TransactionTestCase):
    """
    The organization_has_members constraint trigger: a live organization that
    has had members can never return to zero members. Uses TransactionTestCase
    because the trigger is deferred to commit, which plain TestCase never does.
    """

    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)

    def test_cannot_remove_last_member(self):
        with self.assertRaises(IntegrityError):
            self.org_user.delete()
        self.assertTrue(
            self.organization.users.filter(id=self.user.id).exists(),
            "Failed delete must roll back, keeping the member",
        )

    def test_cannot_cascade_delete_last_member_via_user(self):
        """Deleting a user account cascades to memberships; the cascade must
        not empty a live organization even when Python-level owner checks are
        bypassed."""
        with self.assertRaises(IntegrityError):
            self.user.delete()

    def test_can_remove_member_when_others_remain(self):
        other_user = baker.make("users.user")
        self.organization.add_user(other_user)
        self.org_user.delete()
        self.assertEqual(self.organization.users.count(), 1)

    def test_pending_invite_counts_as_member(self):
        baker.make(
            "organizations_ext.OrganizationUser",
            organization=self.organization,
            user=None,
            email="invited@example.com",
        )
        self.org_user.delete()

    def test_soft_deleted_organization_is_exempt(self):
        """The async cleanup task deletes members while the soft-deleted org
        row still exists."""
        self.organization.is_deleted = True
        self.organization.save(update_fields=["is_deleted"])
        self.org_user.delete()
        self.assertEqual(self.organization.users.count(), 0)

    def test_hard_delete_organization(self):
        """force_delete removes members and the org row in one transaction;
        the deferred check passes because the org row is gone at commit."""
        self.organization.force_delete()
        self.assertFalse(Organization.objects.filter(id=self.organization.id).exists())

    def test_member_swap_in_one_transaction(self):
        """Deferred timing tolerates delete-then-replace within a transaction."""
        new_user = baker.make("users.user")
        with transaction.atomic():
            self.org_user.delete()
            self.organization.add_user(new_user)
        self.assertEqual(self.organization.users.count(), 1)

    def test_memberless_organization_bootstrap(self):
        """Organizations may start with zero members (e.g. created in the
        admin before any user exists) and be claimed by their first user."""
        organization = baker.make("organizations_ext.Organization")
        user = baker.make("users.user")
        org_user = organization.add_user(user)
        self.assertEqual(org_user.role, OrganizationUserRole.OWNER)


class MemberGuardEndpointTestCase(TransactionTestCase):
    """
    Endpoint behavior around the organization_has_members trigger. Uses
    TransactionTestCase so the deferred trigger actually fires on commit.
    """

    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)

    def test_delete_user_owning_one_org_member_of_another(self):
        """A sole owner must not be able to delete their account through a
        plain membership in a second organization, orphaning the org they
        own."""
        other_org = baker.make("organizations_ext.Organization")
        other_owner = baker.make("users.user")
        other_org.add_user(other_owner)
        other_org.add_user(self.user)  # plain member

        self.client.force_login(self.user)
        res = self.client.delete(reverse("api:delete_user", args=["me"]))
        self.assertEqual(res.status_code, 400)
        self.assertIn("owner", res.json()["detail"])
        self.assertTrue(User.objects.filter(id=self.user.id).exists())
        self.assertEqual(self.organization.users.count(), 1)

    def test_delete_user_last_member_without_owner_record(self):
        """Without an OrganizationOwner row the friendly owner check cannot
        fire; the trigger backstop must still surface as a 400, not a 500."""
        OrganizationOwner.objects.filter(organization=self.organization).delete()

        self.client.force_login(self.user)
        res = self.client.delete(reverse("api:delete_user", args=["me"]))
        self.assertEqual(res.status_code, 400)
        self.assertIn("only member", res.json()["detail"])
        self.assertTrue(User.objects.filter(id=self.user.id).exists())

    def test_remove_last_member_without_owner_record(self):
        OrganizationOwner.objects.filter(organization=self.organization).delete()

        self.client.force_login(self.user)
        res = self.client.delete(
            reverse(
                "api:delete_organization_member",
                args=[self.organization.slug, self.org_user.id],
            )
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("last member", res.json()["detail"])
        self.assertEqual(self.organization.users.count(), 1)
