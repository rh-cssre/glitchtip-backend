import requests
import tablib
from django.db.models import Q
from django.urls import reverse

from organizations_ext.admin import OrganizationResource, OrganizationUserResource
from organizations_ext.models import OrganizationUser, OrganizationUserRole
from projects.admin import ProjectKeyResource, ProjectResource
from projects.models import Project
from teams.admin import TeamResource
from users.admin import UserResource
from users.models import User

from .exceptions import ImporterException


class GlitchTipImporter:
    """
    Generic importer tool to use with cli or web

    If used by a non server admin, it's important to assume all incoming
    JSON is hostile and not from a real GT server. Foreign Key ids could be
    faked and used to elevate privileges. Always confirm new data is associated with
    appropriate organization. Also assume user is at least an org admin, no need to
    double check permissions when creating assets within the organization.

    create_users should be False unless running as superuser/management command
    """

    def __init__(
        self, url: str, auth_token: str, organization_slug: str, create_users=False
    ):
        self.api_root_url = reverse("api-root-view")
        self.url = url
        self.headers = {"Authorization": f"Bearer {auth_token}"}
        self.create_users = create_users
        self.organization_slug = organization_slug
        self.organization_id = None
        self.organization_url = reverse(
            "organization-detail", kwargs={"slug": self.organization_slug}
        )
        self.organization_users_url = reverse(
            "organization-users-list",
            kwargs={"organization_slug": self.organization_slug},
        )
        self.projects_url = reverse(
            "organization-projects-list",
            kwargs={"organization_slug": self.organization_slug},
        )
        self.teams_url = reverse(
            "organization-teams-list",
            kwargs={"organization_slug": self.organization_slug},
        )

    def run(self, organization_id=None):
        """Set organization_id to None to import (superuser only)"""
        if organization_id is None:
            self.import_organization()
        else:
            self.organization_id = organization_id
        self.import_organization_users()
        self.import_projects()
        self.import_teams()

    def get(self, url):
        return requests.get(url, headers=self.headers)

    def import_organization(self):
        resource = OrganizationResource()
        res = self.get(self.url + self.organization_url)
        data = res.json()
        self.organization_id = data["id"]  # TODO unsafe for web usage
        dataset = tablib.Dataset()
        dataset.dict = [data]
        resource.import_data(dataset, raise_errors=True)

    def import_organization_users(self):
        resource = OrganizationUserResource()
        res = self.get(self.url + self.organization_users_url)
        org_users = res.json()
        if self.create_users:
            user_resource = UserResource()
            users_list = [
                org_user["user"] for org_user in org_users if org_user is not None
            ]
            users = [
                {k: v for k, v in user.items() if k in ["id", "email", "name"]}
                for user in users_list
            ]
            dataset = tablib.Dataset()
            dataset.dict = users
            user_resource.import_data(dataset, raise_errors=True)

        for org_user in org_users:
            org_user["organization"] = self.organization_id
            org_user["role"] = OrganizationUserRole.from_string(org_user["role"])
            if self.create_users:
                org_user["user"] = (
                    User.objects.filter(email=org_user["user"]["email"])
                    .values_list("pk", flat=True)
                    .first()
                )
            else:
                org_user["user"] = None
        dataset = tablib.Dataset()
        dataset.dict = org_users
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
                key["project"] = project["id"]
                key["public_key"] = key["public"]
            project_keys += keys
        dataset = tablib.Dataset()
        dataset.dict = projects
        project_resource.import_data(dataset, raise_errors=True)
        owned_project_ids = Project.objects.filter(
            organization_id=self.organization_id,
            pk__in=[d["projectId"] for d in project_keys],
        ).values_list("pk", flat=True)
        project_keys = list(
            filter(lambda key: key["projectId"] in owned_project_ids, project_keys)
        )
        dataset.dict = project_keys
        project_key_resource.import_data(dataset, raise_errors=True)

    def import_teams(self):
        resource = TeamResource()
        res = self.get(self.url + self.teams_url)
        teams = res.json()
        for team in teams:
            team["organization"] = self.organization_id
            team["projects"] = ",".join(
                map(
                    str,
                    Project.objects.filter(
                        organization_id=self.organization_id,
                        pk__in=[int(d["id"]) for d in team["projects"]],
                    ).values_list("id", flat=True),
                )
            )
            team_members = self.get(
                self.url
                + reverse(
                    "team-members-list",
                    kwargs={"team_pk": f"{self.organization_slug}/{team['slug']}"},
                )
            ).json()
            team_member_emails = [d["email"] for d in team_members]
            team["members"] = ",".join(
                [
                    str(i)
                    for i in OrganizationUser.objects.filter(
                        organization_id=self.organization_id
                    )
                    .filter(
                        Q(email__in=team_member_emails)
                        | Q(user__email__in=team_member_emails)
                    )
                    .values_list("pk", flat=True)
                ]
            )
        dataset = tablib.Dataset()
        dataset.dict = teams
        resource.import_data(dataset, raise_errors=True)

    def check_auth(self):
        res = requests.get(self.url + self.api_root_url, headers=self.headers)
        data = res.json()
        if res.status_code != 200 or not data["user"]:
            raise ImporterException("Bad auth token")
