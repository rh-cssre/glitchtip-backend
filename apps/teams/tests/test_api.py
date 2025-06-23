from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole

from ..models import Team


class TeamAPITestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)
        self.client.force_login(self.user)

    def test_retrieve(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:get_team", args=[self.organization.slug, team.slug])
        res = self.client.get(url)
        self.assertContains(res, team.slug)

    def test_delete(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:delete_team", args=[self.organization.slug, team.slug])
        res = self.client.delete(url)
        self.assertTrue(res.status_code, 204)
        self.assertFalse(Team.objects.exists())

        team = baker.make("teams.Team", organization=self.organization)
        self.org_user.role = OrganizationUserRole.MEMBER
        self.org_user.save()
        url = reverse("api:delete_team", args=[self.organization.slug, team.slug])
        res = self.client.delete(url)
        self.assertTrue(res.status_code, 404)
        self.assertTrue(Team.objects.exists())

    def test_update(self):
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse("api:update_team", args=[self.organization.slug, team.slug])
        slug = "newslug"
        res = self.client.put(url, data={"slug": slug}, content_type="application/json")
        self.assertContains(res, slug)
        team.refresh_from_db()
        self.assertEqual(team.slug, slug)

    def test_list(self):
        url = reverse("api:list_teams", args=[self.organization.slug])
        project = baker.make("projects.Project", organization=self.organization)
        team = baker.make(
            "teams.Team", organization=self.organization, projects=[project]
        )
        other_organization = baker.make("organizations_ext.Organization")
        other_organization.add_user(self.user)
        other_team = baker.make("teams.Team", organization=other_organization)
        res = self.client.get(url)
        self.assertContains(res, team.slug)
        self.assertContains(res, project.slug)
        self.assertNotContains(res, other_team.slug)

    def test_create(self):
        url = reverse("api:create_team", args=[self.organization.slug])
        data = {"slug": "te$m"}
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data["slug"] = "t" * 51
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data["slug"] = "team"
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, data["slug"], status_code=201)
        self.assertTrue(Team.objects.filter(slug=data["slug"]).exists())

    def test_unauthorized_create(self):
        """Only admins can create teams for that org"""
        data = {"slug": "team"}
        organization = baker.make("organizations_ext.Organization")
        url = reverse("api:list_teams", args=[organization.slug])
        res = self.client.post(url, data)
        # Not even in this org
        self.assertEqual(res.status_code, 400)

        admin_user = baker.make("users.user")
        organization.add_user(admin_user)  # First user is always admin
        organization.add_user(self.user)
        res = self.client.post(url, data)
        # Not an admin
        self.assertEqual(res.status_code, 400)

    def test_invalid_create(self):
        url = reverse("api:list_teams", args=["haha"])
        data = {"slug": "team"}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)

    def test_add_member_to_team(self):
        team = baker.make("teams.Team", organization=self.organization)
        org_user = baker.make(
            "organizations_ext.OrganizationUser", organization=self.organization
        )
        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, 9**9, team.slug],
            )
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(team.members.count(), 0)

        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, "me", team.slug],
            )
        )
        self.assertEqual(res.status_code, 201)
        res = self.client.post(
            reverse(
                "api:add_member_to_team",
                args=[self.organization.slug, org_user.id, team.slug],
            )
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(team.members.count(), 2)

    def test_delete_member_from_team(self):
        team = baker.make(
            "teams.Team", organization=self.organization, members=[self.org_user]
        )

        # Make sure correct org user is selected if user has more than one org
        other_org = baker.make("organizations_ext.Organization")
        baker.make(
            "organizations_ext.OrganizationUser", user=self.user, organization=other_org
        )

        res = self.client.delete(
            reverse(
                "api:delete_member_from_team",
                args=[self.organization.slug, "me", team.slug],
            )
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(team.members.count(), 0)

    def test_organization_users_add_team_member_permission(self):
        self.org_user.role = OrganizationUserRole.MEMBER
        self.org_user.save()
        team = baker.make("teams.Team", organization=self.organization)

        url = reverse(
            "api:add_member_to_team",
            args=[self.organization.slug, self.org_user.id, team.slug],
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
        url = reverse(
            "api:add_member_to_team",
            args=[self.organization.slug, other_org_user.id, team.slug],
        )
        res = self.client.post(url)
        self.assertEqual(res.status_code, 403)

        # Can't add someone when admin and not in team
        self.org_user.role = OrganizationUserRole.ADMIN
        self.org_user.save()
        res = self.client.post(url)
        self.assertEqual(res.status_code, 403)

        # Can add someone when admin and in team
        team.members.add(self.org_user)
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)
        team.members.remove(self.org_user)
        team.members.remove(other_org_user)

        # Can add someone else when manager
        self.org_user.role = OrganizationUserRole.MANAGER
        self.org_user.save()
        res = self.client.post(url)
        self.assertEqual(res.status_code, 201)

    def test_list_project_teams(self):
        project = baker.make("projects.Project", organization=self.organization)
        url = reverse(
            "api:list_project_teams", args=[self.organization.slug, project.slug]
        )
        team = baker.make(
            "teams.Team", organization=self.organization, projects=[project]
        )
        other_team = baker.make("teams.Team", organization=self.organization)
        res = self.client.get(url)
        self.assertContains(res, team.slug)
        self.assertNotContains(res, other_team.slug)
        self.assertNotContains(res, "projects")  # Should not have projects relationship

    def test_add_team_to_project(self):
        new_project = baker.make("projects.Project", organization=self.organization)
        team = baker.make("teams.Team", organization=self.organization)
        url = reverse(
            "api:add_team_to_project",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": new_project.slug,
                "team_slug": team.slug,
            },
        )
        self.assertFalse(new_project.teams.exists())
        res = self.client.post(url, content_type="application/json")
        self.assertContains(res, new_project.slug, status_code=201)
        self.assertTrue(new_project.teams.exists())

    def test_team_add_project_no_perms(self):
        """User must be manager or above to manage project teams"""
        team = baker.make("teams.Team", organization=self.organization)
        new_project = baker.make("projects.Project", organization=self.organization)
        user = baker.make("users.user")
        self.client.force_login(user)
        self.organization.add_user(user, OrganizationUserRole.MEMBER)
        url = reverse(
            "api:add_team_to_project",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": new_project.slug,
                "team_slug": team.slug,
            },
        )
        self.client.post(url)
        self.assertFalse(new_project.teams.exists())

    def test_delete_team_from_project(self):
        project = baker.make("projects.Project", organization=self.organization)
        team = baker.make(
            "teams.Team", organization=self.organization, projects=[project]
        )
        url = reverse(
            "api:delete_team_from_project",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": project.slug,
                "team_slug": team.slug,
            },
        )
        self.assertTrue(project.teams.exists())
        res = self.client.delete(url)
        self.assertContains(res, project.slug)
        self.assertFalse(project.teams.exists())
