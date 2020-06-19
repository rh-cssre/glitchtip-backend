from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from ..models import OrganizationUserRole, OrganizationUser


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

    def test_organization_users_list(self):
        res = self.client.get(self.users_url)
        self.assertContains(res, self.user.email)
        res = self.client.get(self.members_url)
        self.assertContains(res, self.user.email)

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

        url = reverse(
            "organization-members-detail",
            kwargs={
                "organization_slug": self.organization.slug,
                "pk": self.org_user.pk,
            },
        )
        res = self.client.get(url)
        self.assertContains(res, self.user.email)
        self.assertNotContains(res, other_user.email)

        url = reverse(
            "organization-members-detail",
            kwargs={
                "organization_slug": other_organization.slug,
                "pk": other_org_user.pk,
            },
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

    def test_organization_users_add_team_member(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = (
            reverse(
                "organization-members-detail",
                kwargs={
                    "organization_slug": self.organization.slug,
                    "pk": self.org_user.pk,
                },
            )
            + f"teams/{team.slug}/"
        )

        self.assertEqual(team.members.count(), 0)
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(team.members.count(), 1)

        res = self.client.delete(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(team.members.count(), 0)

    def test_organization_users_add_team_member_permission(self):
        self.org_user.role = OrganizationUserRole.MEMBER
        self.org_user.save()
        team = baker.make("teams.Team", organization=self.organization)

        url = (
            reverse(
                "organization-members-detail",
                kwargs={
                    "organization_slug": self.organization.slug,
                    "pk": self.org_user.pk,
                },
            )
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
            reverse(
                "organization-members-detail",
                kwargs={
                    "organization_slug": self.organization.slug,
                    "pk": other_org_user.pk,
                },
            )
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
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        res = self.client.post(self.members_url, data)
        self.assertContains(res, data["email"], status_code=201)
        self.assertEqual(res.data["role"], "manager")
        self.assertTrue(
            OrganizationUser.objects.filter(
                organization=self.organization,
                pending=True,
                email=data["email"],
                user=None,
                role=OrganizationUserRole.MANAGER,
            ).exists()
        )

    def test_organization_users_create_without_permissions(self):
        """ Admin cannot add users to org """
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        data = {
            "email": "new@example.com",
            "role": OrganizationUserRole.MANAGER.label.lower(),
            "teams": [],
            "user": "new@example.com",
        }
        res = self.client.post(self.members_url, data)
        self.assertEquals(res.status_code, 403)
