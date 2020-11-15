from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase


class EnvironmentTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_environments(self):
        url = reverse(
            "organization-environments-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        environment = baker.make(
            "environments.Environment", organization=self.organization
        )
        other_environment = baker.make("environments.Environment")

        res = self.client.get(url)
        self.assertContains(res, environment.name)
        self.assertNotContains(res, other_environment.name)


class EnvironmentProjectTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.project = baker.make("projects.Project", organization=self.organization)

    def test_environment_projects(self):
        url = reverse(
            "project-environments-list",
            kwargs={"project_pk": f"{self.organization.slug}/{self.project.slug}"},
        )
        environment_project = baker.make(
            "environments.EnvironmentProject",
            project=self.project,
            environment__organization=self.organization,
        )
        other_environment_project = baker.make("environments.EnvironmentProject")
        another_environment_project = baker.make(
            "environments.EnvironmentProject",
            environment__organization=self.organization,
        )

        res = self.client.get(url)
        self.assertContains(res, environment_project.environment.name)
        self.assertNotContains(res, other_environment_project.environment.name)
        self.assertNotContains(res, another_environment_project.environment.name)
