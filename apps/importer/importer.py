import aiohttp
import tablib
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.urls import reverse

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import OrganizationUser
from apps.organizations_ext.resources import (
    OrganizationResource,
    OrganizationUserResource,
)
from apps.projects.models import Project
from apps.projects.resources import ProjectKeyResource, ProjectResource
from apps.teams.resources import TeamResource
from apps.users.models import User
from apps.users.resources import UserResource

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
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {auth_token}"}
        self.create_users = create_users
        self.organization_slug = organization_slug
        self.organization_id = None
        self.organization_url = reverse(
            "api:get_organization", args=[self.organization_slug]
        )
        self.organization_users_url = reverse(
            "api:list_organization_members",
            kwargs={"organization_slug": self.organization_slug},
        )
        self.projects_url = reverse(
            "api:list_organization_projects", args=[self.organization_slug]
        )
        self.teams_url = reverse("api:list_teams", args=[self.organization_slug])

    async def run(self, organization_id=None):
        """Set organization_id to None to import (superuser only)"""
        if organization_id is None:
            await self.import_organization()
        else:
            self.organization_id = organization_id
        await self.import_organization_users()
        await self.import_projects()
        await self.import_teams()

    async def get(self, url: str):
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.get(url, headers=self.headers) as res:
                return await res.json()

    async def import_organization(self):
        resource = OrganizationResource()
        data = await self.get(self.url + self.organization_url)
        self.organization_id = data["id"]  # TODO unsafe for web usage
        dataset = tablib.Dataset()
        dataset.dict = [data]
        await sync_to_async(resource.import_data)(dataset, raise_errors=True)

    async def import_organization_users(self):
        resource = OrganizationUserResource()
        org_users = await self.get(self.url + self.organization_users_url)
        if not org_users:
            return
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
            await sync_to_async(user_resource.import_data)(dataset, raise_errors=True)

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
        await sync_to_async(resource.import_data)(dataset, raise_errors=True)

    async def import_projects(self):
        project_resource = ProjectResource()
        project_key_resource = ProjectKeyResource()
        projects = await self.get(self.url + self.projects_url)
        project_keys = []
        for project in projects:
            project["organization"] = self.organization_id
            keys = await self.get(
                self.url
                + reverse(
                    "api:list_project_keys",
                    args=[self.organization_slug, project["slug"]],
                )
            )
            for key in keys:
                key["project"] = project["id"]
                key["public_key"] = key["public"]
            project_keys += keys
        dataset = tablib.Dataset()
        dataset.dict = projects
        await sync_to_async(project_resource.import_data)(dataset, raise_errors=True)
        owned_project_ids = [
            pk
            async for pk in Project.objects.filter(
                organization_id=self.organization_id,
                pk__in=[d["projectId"] for d in project_keys],
            ).values_list("pk", flat=True)
        ]
        project_keys = list(
            filter(lambda key: key["projectId"] in owned_project_ids, project_keys)
        )
        dataset.dict = project_keys
        await sync_to_async(project_key_resource.import_data)(
            dataset, raise_errors=True
        )

    async def import_teams(self):
        resource = TeamResource()
        teams = await self.get(self.url + self.teams_url)
        for team in teams:
            team["organization"] = self.organization_id
            team["projects"] = ",".join(
                map(
                    str,
                    [
                        pk
                        async for pk in Project.objects.filter(
                            organization_id=self.organization_id,
                            pk__in=[int(d["id"]) for d in team["projects"]],
                        ).values_list("id", flat=True)
                    ],
                )
            )
            team_members = await self.get(
                self.url
                + reverse(
                    "api:list_team_organization_members",
                    args=[self.organization_slug, team["slug"]],
                )
            )
            team_member_emails = [d["email"] for d in team_members]
            team["members"] = ",".join(
                [
                    str(i)
                    async for i in OrganizationUser.objects.filter(
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
        await sync_to_async(resource.import_data)(dataset, raise_errors=True)

    async def check_auth(self):
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.get(self.url + "/api/0/", headers=self.headers) as res:
                data = await res.json()
                if res.status != 200 or not data["user"]:
                    raise ImporterException("Bad auth token")
