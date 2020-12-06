from django.urls import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase
from ..models import OrganizationUserRole


class OrganizationProjectsViewTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        organization = baker.make("organizations_ext.Organization")
        organization.add_user(self.user, OrganizationUserRole.ADMIN)
        team = baker.make("teams.Team", organization=self.organization)
        team.members.add(self.org_user)
        self.project.team_set.add(team)
        self.url = reverse(
            "organization-projects-list",
            kwargs={"organization_slug": self.organization.slug},
        )

    def test_organization_projects_list(self):
        with self.assertNumQueries(8):
            res = self.client.get(self.url)
        self.assertContains(res, self.organization.slug)
        self.assertContains(res, self.team.slug)

    def test_organization_projects_list_query(self):
        other_team = baker.make("teams.Team", organization=self.organization)
        other_team.members.add(self.org_user)
        other_project = baker.make("projects.Project", organization=self.organization)
        other_project.team_set.add(other_team)

        res = self.client.get(self.url + "?query=team:" + self.team.slug)
        self.assertContains(res, self.team.slug)
        self.assertNotContains(res, other_team.slug)

        res = self.client.get(self.url + "?query=!team:" + self.team.slug)
        self.assertNotContains(res, self.team.slug)
        self.assertContains(res, other_team.slug)
