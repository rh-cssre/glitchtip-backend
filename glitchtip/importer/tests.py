import requests_mock
from django.core.management import call_command
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase
from projects.models import Project
from teams.models import Team

from .importer import GlitchTipImporter

test_project = {"id": "1", "slug": "project", "name": "project"}
test_key = {
    "id": "a" * 32,
    "public": "a" * 32,
    "projectId": 1,
    "label": "Default",
}


class ImporterTestCase(GlitchTipTestCase):
    def setUp(self):
        self.url = "https://example.com"
        self.org_name = "org"
        self.auth_token = "token"
        self.importer = GlitchTipImporter(
            self.url.lstrip("htps:/"), self.auth_token, self.org_name
        )

    def set_mocks(self, m):
        m.get(self.url + self.importer.api_root_url, json={"user": {"username": "foo"}})
        m.get(self.url + self.importer.organization_url, json={"id": 1})
        m.get(self.url + self.importer.organization_users_url, json=[])
        m.get(self.url + self.importer.projects_url, json=[test_project])
        m.get(self.url + "/api/0/projects/org/project/keys/", json=[test_key])
        m.get(
            self.url + self.importer.teams_url,
            json=[
                {
                    "id": "1",
                    "slug": "team",
                    "projects": [test_project],
                }
            ],
        )
        m.get(self.url + "/api/0/teams/org/team/members/", json=[])

    @requests_mock.Mocker()
    def test_import_command(self, m):
        self.set_mocks(m)

        call_command("import", self.url, self.auth_token, self.org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                slug=test_project["slug"],
                team__slug="team",
                projectkey__public_key=test_key["public"],
            ).exists()
        )

    @requests_mock.Mocker()
    def test_view(self, m):
        self.create_user_and_project()
        self.organization.slug = self.org_name
        self.organization.save()
        self.set_mocks(m)
        url = reverse("import")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": self.org_name,
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Team.objects.filter(slug="team").exists())

    @requests_mock.Mocker()
    def test_invalid_org(self, m):
        self.create_user_and_project()
        url = reverse("import")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": "foo",
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)
        other_user = baker.make("users.User")
        other_org = baker.make("Organization", name="foo")
        other_org.add_user(other_user)
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)
        org_user = other_org.add_user(self.user)
        m.get(self.url + self.importer.api_root_url, json={"user": {"username": "foo"}})
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 400)
