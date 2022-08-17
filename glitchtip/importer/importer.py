import requests
import tablib
from django.urls import reverse

from organizations_ext.admin import OrganizationResource
from projects.admin import ProjectResource


from .exceptions import ImporterException


class GlitchTipImporter:
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
        self.check_auth()

    def run(self):
        self.check_auth()
        self.import_organization()
        self.import_projects()

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
        resource = ProjectResource()
        res = self.get(self.url + self.projects_url)
        data = res.json()
        for obj in data:
            obj["organization"] = self.organization_id
        dataset = tablib.Dataset()
        dataset.dict = data
        resource.import_data(dataset, raise_errors=True)

    def check_auth(self):
        res = requests.get(self.url + self.api_root_url, headers=self.headers)
        data = res.json()
        if res.status_code != 200 or not data["user"]:
            raise ImporterException("Bad auth token")
