import requests
import tablib
from django.urls import reverse

from organizations_ext.admin import OrganizationResource
from projects.admin import ProjectKeyResource, ProjectResource
from teams.admin import TeamResource

from .exceptions import ImporterException


class GlitchTipImporter:
    """
    Generic importer tool to use with cli or web

    If used by a non server admin, it's important to assume all incoming
    JSON is hostile and not from a real GT server. Foreign Key ids could be
    faked and used to elevate privileges. Always confirm new data is associated with
    appropriate organization. Also assume user is at least an org admin, no need to
    double check permissions when creating assets within the organization.
    """

    def __init__(self, url: str, auth_token: str, organization_slug: str):
        self.api_root_url = reverse("api-root-view")
        self.url = url
        self.headers = {"Authorization": f"Bearer {auth_token}"}
        self.organization_slug = organization_slug
        self.organization_id = None
        self.organization_url = reverse(
            "organization-detail", kwargs={"slug": self.organization_slug}
        )
        self.projects_url = reverse(
            "organization-projects-list",
            kwargs={"organization_slug": self.organization_slug},
        )
        self.teams_url = reverse(
            "organization-teams-list",
            kwargs={"organization_slug": self.organization_slug},
        )
        self.check_auth()

    def run(self):
        self.check_auth()
        self.import_organization()
        # self.import_projects()
        self.import_teams()

    def get(self, url):
        return requests.get(url, headers=self.headers)

    def import_organization(self):
        resource = OrganizationResource()
        res = self.get(self.url + self.organization_url)
        data = res.json()
        self.organization_id = data["id"]
        dataset = tablib.Dataset()
        dataset.dict = [data]
        resource.import_data(dataset, raise_errors=True)

    def import_projects(self):
        project_resource = ProjectResource()
        project_key_resource = ProjectKeyResource()
        res = self.get(self.url + self.projects_url)
        projects = res.json()
        project_keys = []
        for project in projects:
            project["organization"] = self.organization_id
            keys = self.get(
                self.url
                + reverse(
                    "project-keys-list",
                    kwargs={
                        "project_pk": f"{self.organization_slug}/{project['slug']}",
                    },
                )
            ).json()
            for key in keys:
                # TODO unsafe if used by non-admin, this value could be ANY project
                key["project"] = project["id"]
                key["public_key"] = key["public"]
            project_keys += keys
        dataset = tablib.Dataset()
        dataset.dict = projects
        project_resource.import_data(dataset, raise_errors=True)
        dataset.dict = project_keys
        project_key_resource.import_data(dataset, raise_errors=True)

    def import_teams(self):
        resource = TeamResource()
        res = self.get(self.url + self.teams_url)
        teams = res.json()
        for team in teams:
            team["organization"] = self.organization_id
            # team["projects"] = ",".join([d["id"] for d in team["projects"]])
            team["projects"] = [d["id"] for d in team["projects"]]
        dataset = tablib.Dataset()
        print(teams)
        dataset.dict = teams
        resource.import_data(dataset, raise_errors=True)

    def check_auth(self):
        res = requests.get(self.url + self.api_root_url, headers=self.headers)
        data = res.json()
        if res.status_code != 200 or not data["user"]:
            raise ImporterException("Bad auth token")
