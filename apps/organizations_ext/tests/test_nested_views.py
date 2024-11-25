from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


class OrganizationProjectsViewTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.url = reverse(
            "api:list_organization_projects", args=[self.organization.slug]
        )

    def test_organization_projects_list(self):
        with self.assertNumQueries(2):
            res = self.client.get(self.url)
        self.assertNotContains(res, self.organization.slug)
        self.assertContains(res, self.team.slug)
        self.assertIsInstance(res.json()[0]["teams"][0]["id"], str)

    def test_organization_projects_list_query(self):
        other_team = baker.make("teams.Team", organization=self.organization)
        other_team.members.add(self.org_user)
        other_project = baker.make("projects.Project", organization=self.organization)
        other_project.teams.add(other_team)

        res = self.client.get(self.url + "?query=team:" + self.team.slug)
        self.assertContains(res, self.team.slug)
        self.assertNotContains(res, other_team.slug)

        res = self.client.get(self.url + "?query=!team:" + self.team.slug)
        self.assertNotContains(res, self.team.slug)
        self.assertContains(res, other_team.slug)
