from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..models import EnvironmentProject


class EnvironmentTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.url = reverse("api:list_environments", args=[self.organization.slug])

    def test_environments(self):
        environment = baker.make(
            "environments.Environment", organization=self.organization
        )
        baker.make(
            "environments.EnvironmentProject",
            environment=environment,
            project=self.project,
        )
        other_environment = baker.make("environments.Environment")
        baker.make(
            "environments.EnvironmentProject",
            environment=other_environment,
            project=self.project,
        )

        res = self.client.get(self.url)
        self.assertContains(res, environment.name)
        self.assertNotContains(res, other_environment.name)

    def test_hide_environments(self):
        environment_project1 = baker.make(
            "environments.EnvironmentProject",
            project=self.project,
            environment__organization=self.organization,
            is_hidden=False,
        )
        environment_project2 = baker.make(
            "environments.EnvironmentProject",
            project=self.project,
            environment__organization=self.organization,
            is_hidden=True,
        )
        res = self.client.get(self.url + "?visibility=visible")
        self.assertContains(res, environment_project1.environment.name)
        self.assertNotContains(res, environment_project2.environment.name)


class EnvironmentProjectTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()

    def test_environment_projects(self):
        url = reverse(
            "api:list_environment_projects",
            args=[self.organization.slug, self.project.slug],
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

    def test_make_hidden(self):
        environment_project = baker.make(
            "environments.EnvironmentProject",
            is_hidden=False,
            project=self.project,
            environment__organization=self.organization,
        )
        detail_url = reverse(
            "api:update_environment_project",
            args=[
                self.organization.slug,
                self.project.slug,
                environment_project.environment.name,
            ],
        )
        data = {"name": environment_project.environment.name, "isHidden": True}
        res = self.client.put(detail_url, data, content_type="application/json")
        self.assertContains(res, "true")
        self.assertTrue(EnvironmentProject.objects.filter(is_hidden=True).exists())
