from django.urls import reverse
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCase


class ProjectTeamViewTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse(
            "project-teams-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )

    def test_project_team_list(self):
        res = self.client.get(self.url)
        self.assertContains(res, self.team.slug)

    def test_project_team_add_project(self):
        new_project = baker.make("projects.Project", organization=self.organization)
        url = reverse(
            "project-teams-list",
            kwargs={"project_pk": self.organization.slug + "/" + new_project.slug},
        )
        self.assertFalse(new_project.team_set.exists())
        res = self.client.post(url + self.team.slug + "/")
        self.assertContains(res, new_project.slug, status_code=201)
        self.assertTrue(new_project.team_set.exists())

    def test_project_team_add_project_no_perms(self):
        """ User must be manager or above to manage project teams """
        new_project = baker.make("projects.Project", organization=self.organization)
        user = baker.make("users.user")
        self.client.force_login(user)
        self.organization.add_user(user, OrganizationUserRole.MEMBER)
        url = reverse(
            "project-teams-list",
            kwargs={"project_pk": self.organization.slug + "/" + new_project.slug},
        )
        self.client.post(url + self.team.slug + "/")
        self.assertFalse(new_project.team_set.exists())

    def test_project_team_remove_project(self):
        url = reverse(
            "project-teams-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )
        self.assertTrue(self.project.team_set.exists())
        res = self.client.delete(url + self.team.slug + "/")
        self.assertContains(res, self.project.slug)
        self.assertFalse(self.project.team_set.exists())

