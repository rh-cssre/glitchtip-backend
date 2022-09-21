import requests_mock
from django.core.management import call_command

from glitchtip.test_utils.test_case import GlitchTipTestCase
from projects.models import Project
from teams.models import Team

from .importer import GlitchTipImporter


class ImporterTestCase(GlitchTipTestCase):
    @requests_mock.Mocker()
    def test_import_command(self, m):
        url = "https://example.com"
        org_name = "org"
        auth_token = "token"
        importer = GlitchTipImporter(url.lstrip("htps:/"), auth_token, org_name)

        project = {"id": "1", "slug": "project", "name": "project"}
        key = {
            "id": "a" * 32,
            "public": "a" * 32,
            "projectId": 1,
            "label": "Default",
        }
        m.get(url + importer.api_root_url, json={"user": {"username": "foo"}})
        m.get(url + importer.organization_url, json={"id": 1})
        m.get(url + importer.organization_users_url, json=[])
        m.get(url + importer.projects_url, json=[project])
        m.get(url + "/api/0/projects/org/project/keys/", json=[key])
        m.get(
            url + importer.teams_url,
            json=[
                {
                    "id": "1",
                    "slug": "team",
                    "projects": [project],
                }
            ],
        )
        m.get(url + "/api/0/teams/org/team/members/", json=[])

        call_command("import", url, auth_token, org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                slug=project["slug"],
                team__slug="team",
                projectkey__public_key=key["public"],
            ).exists()
        )
