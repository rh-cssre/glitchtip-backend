import json

from django.core import mail
from django.urls import reverse
from django.test import override_settings
from model_bakery import baker
from rest_framework.test import APITestCase

from glitchtip import test_utils  # pylint: disable=unused-import

from ..models import OrganizationUser, OrganizationUserRole


class OrganizationUsersAPITestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(
            self.user, role=OrganizationUserRole.MANAGER
        )
        self.client.force_login(self.user)
        self.users_url = reverse(
            "organization-users-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.members_url = reverse(
            "organization-members-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def get_org_member_detail_url(self, organization_slug, pk):
        return reverse(
            "organization-members-detail",
            kwargs={
                "organization_slug": organization_slug,
                "pk": pk,
            },
        )

    def test_organization_users_list(self):
        res = self.client.get(self.users_url)
        self.assertContains(res, self.user.email)
        res = self.client.get(self.members_url)
        self.assertContains(res, self.user.email)

    def test_organization_members_email_field(self):
        """
        Org Member email should refer to the invited email before acceptance
        After acceptance, it should refer to the user's primary email address
        """
        url = self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
        res = self.client.get(url)
        self.assertEqual(res.data["email"], self.user.email)

    def test_organization_team_members_list(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse(
            "team-members-list",
            kwargs={"team_pk": f"{self.organization.slug}/{team.slug}"},
        )
        res = self.client.get(url)
        self.assertNotContains(res, self.user.email)

        team.members.add(self.org_user)
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_organization_users_detail(self):
        other_user = baker.make("users.user")
        other_organization = baker.make("organizations_ext.Organization")
        other_org_user = other_organization.add_user(other_user)
        team = baker.make("teams.Team", organization=self.organization)
        team.members.add(self.org_user)

        url = self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
        res = self.client.get(url)
        self.assertContains(res, self.user.email)
        self.assertContains(res, team.slug)
        self.assertNotContains(res, other_user.email)

        url = self.get_org_member_detail_url(self.organization.slug, "me")
        res = self.client.get(url)
        self.assertContains(res, self.user.email)
        self.assertNotContains(res, other_user.email)

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

    def test_organization_users_add_team_member(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = (
            self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
            + f"teams/{team.slug}/"
        )

        self.assertEqual(team.members.count(), 0)
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(team.members.count(), 1)

        res = self.client.delete(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(team.members.count(), 0)

    def test_organization_users_add_self_team_member(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = (
            self.get_org_member_detail_url(self.organization.slug, "me")
            + f"teams/{team.slug}/"
        )

        self.assertEqual(team.members.count(), 0)
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(team.members.count(), 1)

        res = self.client.delete(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(team.members.count(), 0)

    def test_organization_users_create_and_accept_invite(self):
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        res = self.client.post(self.members_url, data)
        self.assertTrue(res.data["pending"])
        body = mail.outbox[0].body
        body_split = body[body.find("http://localhost:8000/accept/") :].split("/")
        org_user_id = body_split[4]
        token = body_split[5]
        url = reverse(
            "accept-invite", kwargs={"org_user_id": org_user_id, "token": token}
        )

        # Check that we can determine organization name from GET request to accept invite endpoint
        self.client.logout()
        res = self.client.get(url)
        self.assertContains(res, self.organization.name)

        user = baker.make("users.user")
        self.client.force_login(user)
        data = {"accept_invite": True}
        res = self.client.post(url, data)
        self.assertContains(res, self.organization.name)
        self.assertFalse(res.data["org_user"]["pending"])
        self.assertTrue(
            OrganizationUser.objects.filter(
                user=user, organization=self.organization
            ).exists()
        )

    def test_closed_user_registration(self):
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }

        with override_settings(ENABLE_USER_REGISTRATION=False):
            # Non-existing user cannot be invited
            res = self.client.post(self.members_url, data)
            self.assertEqual(res.status_code, 403)

            # Existing user can be invited
            self.user = baker.make("users.user", email="new@example.com")
            res = self.client.post(self.members_url, data)
            self.assertEqual(res.status_code, 201)

    def test_organization_users_invite_twice(self):
        """Don't allow inviting user who is already in the group"""
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        res = self.client.post(self.members_url, data)
        self.assertEqual(res.status_code, 201)
        res = self.client.post(self.members_url, data)
        self.assertEqual(res.status_code, 409)
        data["email"] = self.user.email
        res = self.client.post(self.members_url, data)
        self.assertEqual(res.status_code, 409)

    def test_organization_users_add_team_member_permission(self):
        self.org_user.role = OrganizationUserRole.MEMBER
        self.org_user.save()
        team = baker.make("teams.Team", organization=self.organization)

        url = (
            self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
            + f"teams/{team.slug}/"
        )

        # Add self with open membership
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)
        res = self.client.delete(url)

        # Can't add self without open membership
        self.organization.open_membership = False
        self.organization.save()
        res = self.client.post(url)
        self.assertEqual(res.status_code, 403)
        self.organization.open_membership = True
        self.organization.save()

        # Can't add someone else with open membership when not admin
        other_user = baker.make("users.User")
        other_org_user = self.organization.add_user(other_user)
        other_org_user_url = (
            self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)
            + f"teams/{team.slug}/"
        )
        res = self.client.post(other_org_user_url)
        self.assertEqual(res.status_code, 403)

        # Can't add someone when admin and not in team
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        res = self.client.post(other_org_user_url)
        self.assertEqual(res.status_code, 403)

        # Can add someone when admin and in team
        team.members.add(self.org_user)
        res = self.client.post(other_org_user_url)
        self.assertEqual(res.status_code, 201)
        team.members.remove(self.org_user)
        team.members.remove(other_org_user)

        # Can add someone else when manager
        self.org_user.role = OrganizationUserRole.MANAGER
        self.org_user.save()
        res = self.client.post(other_org_user_url)
        self.assertEqual(res.status_code, 201)

    def test_organization_users_create(self):
        team = baker.make("teams.Team", organization=self.organization)
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [team.slug],
            "user": "new@example.com",
        }
        res = self.client.post(
            self.members_url, json.dumps(data), content_type="application/json"
        )
        self.assertContains(res, data["email"], status_code=201)
        self.assertEqual(res.data["role"], "manager")
        self.assertTrue(
            OrganizationUser.objects.filter(
                organization=self.organization,
                email=data["email"],
                user=None,
                role=OrganizationUserRole.MANAGER,
            ).exists()
        )
        self.assertTrue(team.members.exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_organization_users_create_and_accept(self):
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        self.client.post(self.members_url, data)
        body = mail.outbox[0].body
        token = body[body.find("http://localhost:8000/accept/") :].split("/")[4]

    def test_organization_users_create_without_permissions(self):
        """Admin cannot add users to org"""
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        res = self.client.post(self.members_url, data)
        self.assertEqual(res.status_code, 403)

    def test_organization_users_reinvite(self):
        other_user = baker.make("users.user")
        other_org_user = baker.make(
            "organizations_ext.OrganizationUser",
            email=other_user.email,
            organization=self.organization,
        )

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)
        data = {"reinvite": 1}
        res = self.client.put(url, data)
        self.assertContains(res, other_user.email)
        self.assertTrue(len(mail.outbox))

    def test_organization_users_update(self):
        other_user = baker.make("users.user")
        other_org_user = self.organization.add_user(other_user)

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)

        new_role = OrganizationUserRole.ADMIN
        data = {"role": new_role.label.lower(), "teams": []}
        res = self.client.put(url, data)
        self.assertContains(res, other_user.email)
        self.assertTrue(
            OrganizationUser.objects.filter(
                organization=self.organization, role=new_role, user=other_user
            ).exists()
        )

    def test_organization_users_update_without_permissions(self):
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        other_user = baker.make("users.user")
        other_org_user = self.organization.add_user(other_user)

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)

        new_role = OrganizationUserRole.ADMIN
        data = {"role": new_role.label.lower(), "teams": []}
        res = self.client.put(url, data)
        self.assertEqual(res.status_code, 403)

    def test_organization_users_delete(self):
        other_user = baker.make("users.user")
        other_org_user = self.organization.add_user(other_user)

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)

        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(other_user.organizations_ext_organizationuser.count(), 0)

        url = self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
        res = self.client.delete(url)
        self.assertEqual(
            res.status_code,
            400,
            "Org owner should not be able to remove themselves from org",
        )

        third_user = baker.make("users.user")
        third_org_user = self.organization.add_user(third_user)
        change_ownership_url = (
            self.get_org_member_detail_url(self.organization.slug, third_org_user.pk)
            + "set_owner/"
        )
        self.client.post(change_ownership_url)

        res = self.client.delete(url)
        self.assertEqual(
            res.status_code,
            204,
            "Can remove self after transferring ownership",
        )

    def test_organization_users_delete_without_permissions(self):
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        other_user = baker.make("users.user")
        other_org_user = self.organization.add_user(other_user)

        url = self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)

        res = self.client.delete(url)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(other_user.organizations_ext_organizationuser.count(), 1)

    def test_organization_members_set_owner(self):
        other_user = baker.make("users.user")
        other_org_user = self.organization.add_user(other_user)
        random_org_user = baker.make("organizations_ext.OrganizationUser")

        url = (
            self.get_org_member_detail_url(self.organization.slug, random_org_user.pk)
            + "set_owner/"
        )
        res = self.client.post(url)
        self.assertEqual(
            res.status_code, 404, "Don't set random unrelated users as owner"
        )

        url = (
            self.get_org_member_detail_url(self.organization.slug, other_org_user.pk)
            + "set_owner/"
        )
        res = self.client.post(url)
        self.assertTrue(
            res.data["isOwner"], "Current owner may set another org member as owner"
        )

        url = (
            self.get_org_member_detail_url(self.organization.slug, self.org_user.pk)
            + "set_owner/"
        )
        self.org_user.role = OrganizationUserRole.MANAGER
        self.org_user.save()
        res = self.client.post(url)
        self.assertEqual(
            res.status_code, 403, "Can't set self as owner with only manager role"
        )

        self.org_user.role = OrganizationUserRole.OWNER
        self.org_user.save()
        res = self.client.post(url)
        self.assertTrue(res.data["isOwner"], "Owner role may set org member as owner")
        self.assertEqual(self.organization.owners.count(), 1)
