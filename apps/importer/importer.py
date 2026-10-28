from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import aiohttp
import tablib
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Lower
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


# Web imports match rows inside the importing org and never take the remote's ids.
class ScopedOrganizationUserResource(OrganizationUserResource):
    class Meta(OrganizationUserResource.Meta):
        fields = ("user", "organization", "role", "email")


class ScopedProjectResource(ProjectResource):
    class Meta(ProjectResource.Meta):
        fields = ("created", "slug", "name", "organization", "platform")
        import_id_fields = ("organization", "slug")


class ScopedTeamResource(TeamResource):
    class Meta(TeamResource.Meta):
        fields = ("slug", "created", "organization", "projects", "members")
        import_id_fields = ("organization", "slug")


@dataclass(frozen=True)
class ImportResources:
    org_user: type[OrganizationUserResource]
    project: type[ProjectResource]
    team: type[TeamResource]
    scoped: bool


# A whole-instance migration keeps ids, so DSN project ids still work.
INSTANCE_RESOURCES = ImportResources(
    OrganizationUserResource, ProjectResource, TeamResource, scoped=False
)
SCOPED_RESOURCES = ImportResources(
    ScopedOrganizationUserResource,
    ScopedProjectResource,
    ScopedTeamResource,
    scoped=True,
)


class GlitchTipImporter:
    """
    Generic importer tool to use with cli or web

    If used by a non server admin, it's important to assume all incoming
    JSON is hostile and not from a real GT server. Foreign Key ids could be
    faked and used to elevate privileges. Always confirm new data is associated with
    appropriate organization. Imported member roles are capped at max_role, the
    importing user's own role.

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
        self.resources = SCOPED_RESOURCES
        self.max_role = OrganizationUserRole.OWNER
        self.organization_url = reverse(
            "api:get_organization", args=[self.organization_slug]
        )
        self.organization_users_url = reverse(
            "api:list_organization_members", args=[self.organization_slug]
        )
        self.projects_url = reverse(
            "api:list_organization_projects", args=[self.organization_slug]
        )
        self.teams_url = reverse("api:list_teams", args=[self.organization_slug])

    async def run(self, organization_id=None, max_role=OrganizationUserRole.OWNER):
        """Set organization_id to None to import (superuser only)"""
        self.max_role = max_role
        if organization_id is None:
            self.resources = INSTANCE_RESOURCES
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
        resource = self.resources.org_user()
        org_users = await self.get(self.url + self.organization_users_url)
        if self.resources.scoped:
            org_users = await self.new_members(org_users)
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
            org_user["role"] = min(
                OrganizationUserRole.from_string(org_user["role"]), self.max_role
            )
            if self.create_users:
                org_user["user"] = user_ids_by_email.get(org_user["user"]["email"])
            else:
                org_user["user"] = None
        dataset = tablib.Dataset()
        dataset.dict = org_users
        await sync_to_async(resource.import_data)(dataset, raise_errors=True)

    def members_by_email(self, emails: list[str | None]):
        """This org's members whose invite or account email matches, ignoring case."""
        lowered = {email.lower() for email in emails if email}
        return (
            OrganizationUser.objects.filter(organization_id=self.organization_id)
            .annotate(email_lower=Lower("email"), user_email_lower=Lower("user__email"))
            .filter(Q(email_lower__in=lowered) | Q(user_email_lower__in=lowered))
        )

    async def new_members(self, org_users: list[dict]) -> list[dict]:
        """Drop rows for emails already in the org; an import never edits a member."""
        taken = {
            email
            async for pair in self.members_by_email(
                [org_user.get("email") for org_user in org_users]
            ).values_list("email_lower", "user_email_lower")
            for email in pair
        }
        new = []
        for org_user in org_users:
            email = (org_user.get("email") or "").lower()
            if email in taken:
                continue
            if email:
                taken.add(email)
            new.append(org_user)
        return new

    async def local_project_ids(self, slugs: list[str]) -> dict[str, int]:
        return {
            slug: pk
            async for slug, pk in Project.objects.filter(
                organization_id=self.organization_id, slug__in=slugs
            ).values_list("slug", "pk")
        }

    async def import_projects(self):
        projects = await self.get(self.url + self.projects_url)
        # import_id_fields other than "id" need headers, so an empty list raises.
        if not projects:
            return
        if self.resources.scoped and (
            deleted := [
                slug
                async for slug in Project.objects.filter(
                    organization_id=self.organization_id,
                    slug__in=[project["slug"] for project in projects],
                    is_deleted=True,
                ).values_list("slug", flat=True)
            ]
        ):
            raise ImporterException(
                "Deleted projects with these slugs are still being removed: "
                f"{', '.join(deleted)}. Retry once they are gone."
            )
        for project in projects:
            project["organization"] = self.organization_id
        dataset = tablib.Dataset()
        dataset.dict = projects
        await sync_to_async(self.resources.project().import_data)(
            dataset, raise_errors=True
        )
        # Web-imported projects get new ids, so their DSNs change regardless;
        # each keeps the fresh key it was created with.
        if not self.resources.scoped:
            await self.import_project_keys([project["slug"] for project in projects])

    async def import_project_keys(self, slugs: list[str]):
        project_ids = await self.local_project_ids(slugs)
        project_keys = []
        for slug in slugs:
            if slug not in project_ids:
                continue
            keys = await self.get(
                self.url
                + reverse("api:list_project_keys", args=[self.organization_slug, slug])
            )
            for key in keys:
                key["project"] = project_ids[slug]
                key["public_key"] = key["public"]
                project_keys.append(key)
        if not project_keys:
            return
        dataset = tablib.Dataset()
        dataset.dict = project_keys
        await sync_to_async(ProjectKeyResource().import_data)(
            dataset, raise_errors=True
        )

    async def import_teams(self):
        resource = self.resources.team()
        teams = await self.get(self.url + self.teams_url)
        if not teams:
            return
        for team in teams:
            team["organization"] = self.organization_id
            project_ids = await self.local_project_ids(
                [d["slug"] for d in team["projects"]]
            )
            team["projects"] = ",".join(map(str, project_ids.values()))
            team_members = await self.get(
                self.url
                + reverse(
                    "api:list_team_organization_members",
                    args=[self.organization_slug, team["slug"]],
                )
            )
            team["members"] = ",".join(
                [
                    str(i)
                    async for i in self.members_by_email(
                        [d["email"] for d in team_members]
                    ).values_list("pk", flat=True)
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
