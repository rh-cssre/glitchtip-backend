from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


class OrganizationProjectsViewTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)
        self.url = reverse(
            "api:list_organization_projects", args=[self.organization.slug]
        )

    def test_organization_projects_list(self):
        with self.assertNumQueries(2):
            res = self.client.get(self.url)
        self.assertNotContains(res, self.organization.slug)
        self.assertContains(res, self.team.slug)
        # Find project with teams
        teams = res.json()[0]["teams"] or res.json()[1]["teams"]
        self.assertIsInstance(teams[0]["id"], str)

    async def test_organization_projects_list_query(self):
        other_team = await baker.amake("teams.Team", organization=self.organization)
        await other_team.members.aadd(self.org_user)
        other_project = await baker.amake(
            "projects.Project", organization=self.organization
        )
        await other_project.teams.aadd(other_team)

        res = await self.async_client.get(self.url + "?query=team:" + self.team.slug)
        self.assertContains(res, self.team.slug)
        self.assertNotContains(res, other_team.slug)

        res = await self.async_client.get(self.url + "?query=!team:" + self.team.slug)
        self.assertNotContains(res, self.team.slug)
        self.assertContains(res, other_team.slug)
