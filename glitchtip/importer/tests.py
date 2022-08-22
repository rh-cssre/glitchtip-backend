import requests_mock
from django.core.management import call_command
from django.urls import reverse

from glitchtip.test_utils.test_case import GlitchTipTestCase
from projects.models import Project
from teams.models import Team

from .importer import GlitchTipImporter


class ImporterTestCase(GlitchTipTestCase):
    def setUp(self):
        self.url = "https://example.com"
        self.org_name = "org"
        self.auth_token = "token"
        self.importer = GlitchTipImporter(
            self.url.lstrip("htps:/"), self.auth_token, self.org_name
        )

    @requests_mock.Mocker()
    def test_import_command(self, m):
        project = {"id": "1", "slug": "project", "name": "project"}
        key = {
            "id": "a" * 32,
            "public": "a" * 32,
            "projectID": 1,
            "label": "Default",
        }
        m.get(self.url + self.importer.api_root_url, json={"user": {"username": "foo"}})
        m.get(self.url + self.importer.organization_url, json={"id": 1})
        m.get(self.url + self.importer.organization_users_url, json=[])
        m.get(self.url + self.importer.projects_url, json=[project])
        m.get(self.url + "/api/0/projects/org/project/keys/", json=[key])
        m.get(
            self.url + self.importer.teams_url,
            json=[
                {
                    "id": "1",
                    "slug": "team",
                    "projects": [project],
                }
            ],
        )
        m.get(self.url + "/api/0/teams/org/team/members/", json=[])

        call_command("import", self.url, self.auth_token, self.org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                slug=project["slug"],
                team__slug="team",
                projectkey__public_key=key["public"],
            ).exists()
        )

    @requests_mock.Mocker()
    def test_view(self, m):
        self.create_user_and_project()
        self.organization.slug = self.org_name
        self.organization.save()
        url = reverse("import")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": self.org_name,
        }
        res = self.client.options(url)
        print(res.data)
        res = self.client.post(url, data)
        print(res.data)
        self.assertEqual(res.status_code, 200)
