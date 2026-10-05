from urllib.parse import urljoin, urlparse

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
from glitchtip.ssrf_protection import (
    request_with_validated_redirects,
    safe_session,
)

from .exceptions import ImporterException

MAX_IMPORTER_PAGES = 100


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
        data = []
        next_url = url
        seen_urls = {url}
        pages_fetched = 0
        allow_private = settings.GLITCHTIP_ALLOW_PRIVATE_IPS
        async with safe_session(allow_private) as session:
            while next_url:
                async with await request_with_validated_redirects(
                    session,
                    "GET",
                    next_url,
                    headers=self.headers,
                    allow_private=allow_private,
                ) as res:
                    response_data = await res.json()
                    if not isinstance(response_data, list):
                        if data:
                            raise ImporterException(
                                "Importer expected a list of results but got "
                                f"{type(response_data).__name__}"
                            )
                        return response_data
                    data.extend(response_data)
                    pages_fetched += 1
                    next_url = self.get_next_url(res, next_url)
                    if next_url:
                        if next_url in seen_urls:
                            # The remote is pointing back at a page already
                            # fetched. GlitchTip's own last page does exactly
                            # this, so treat a repeat as the end of the results
                            # rather than crawling until the page cap.
                            break
                        seen_urls.add(next_url)
                        if pages_fetched >= MAX_IMPORTER_PAGES:
                            raise ImporterException(
                                "Importer pagination exceeded "
                                f"{MAX_IMPORTER_PAGES} pages"
                            )
        return data

    @staticmethod
    def get_next_url(res: aiohttp.ClientResponse, current_url: str) -> str | None:
        link_header = res.headers.get("Link")
        if not link_header:
            return None

        for link in link_header.split(","):
            parts = [part.strip() for part in link.split(";")]
            if not parts or not parts[0].startswith("<") or not parts[0].endswith(">"):
                continue
            link_url = parts[0][1:-1]
            params = {}
            for part in parts[1:]:
                key, _, value = part.partition("=")
                params[key.lower()] = value.strip('"')
            # A remote that omits the flag entirely still gets followed; only an
            # explicit "false" means there is nothing after this page. The loop
            # guard in get() is what bounds a remote whose flag never flips.
            if params.get("rel") == "next" and params.get("results") != "false":
                return GlitchTipImporter.rebase_url(current_url, link_url)
        return None

    @staticmethod
    def rebase_url(current_url: str, link_url: str) -> str:
        """Re-anchor a pagination link onto the origin we are already reading.

        Pagination links are absolute, and a server builds them from whatever it
        believes its own public address to be rather than from the address the
        request arrived on -- a reverse proxy, a port-forward, or plain http in
        front of an https-configured install is enough to make the two differ.
        Following one verbatim would send the caller's auth token to a host the
        operator never named, so take only the path and query from it. The first
        page is fetched at the operator's own URL and every page after it is
        anchored to the page before, so the token cannot leave that origin --
        whether the difference was a misconfigured proxy or a hostile remote
        trying to collect the token.
        """
        parsed = urlparse(link_url)
        path_and_query = parsed.path
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"
        return urljoin(current_url, path_and_query)

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
            # Resolve every imported user in one query. The rows were just
            # written above, so look them up by the email the remote reported
            # rather than trusting its primary keys.
            user_ids_by_email = {
                email: pk
                async for email, pk in User.objects.filter(
                    email__in=[user["email"] for user in users]
                ).values_list("email", "pk")
            }

        for org_user in org_users:
            org_user["organization"] = self.organization_id
            org_user["role"] = OrganizationUserRole.from_string(org_user["role"])
            if self.create_users:
                org_user["user"] = user_ids_by_email.get(org_user["user"]["email"])
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
        allow_private = settings.GLITCHTIP_ALLOW_PRIVATE_IPS
        async with (
            safe_session(allow_private) as session,
            await request_with_validated_redirects(
                session,
                "GET",
                self.url + "/api/0/",
                headers=self.headers,
                allow_private=allow_private,
            ) as res,
        ):
            data = await res.json()
            if res.status != 200 or not data["user"]:
                raise ImporterException("Bad auth token")
