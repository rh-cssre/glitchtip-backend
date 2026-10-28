from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker
from pydantic import ValidationError

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import OrganizationUser
from apps.projects.models import Project
from apps.teams.models import Team
from apps.users.models import User
from glitchtip.test_utils import generators  # noqa: F401
from glitchtip.test_utils.aiohttp_mock import mock_aiohttp
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from .exceptions import ImporterException
from .importer import MAX_IMPORTER_PAGES, GlitchTipImporter
from .schema import ImportIn

test_project = {"id": "1", "slug": "project", "name": "project"}
# Renaming a project keeps its slug, so the two often differ.
renamed_project = {"id": "1", "slug": "backend-api", "name": "Backend"}
test_key = {
    "id": "a" * 32,
    "public": "a" * 32,
    "projectId": 1,
    "label": "Default",
}
test_org_user = {
    "id": "1",
    "email": "member@example.com",
    "role": "owner",
    "user": {"id": "1", "email": "member@example.com", "name": "Member"},
}


class ImporterTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.url = "https://example.com"
        self.org_name = "org"
        self.auth_token = "token"
        self.importer = GlitchTipImporter(
            self.url.lstrip("htps:/"), self.auth_token, self.org_name
        )

    def set_mocks(
        self, m, org_users=None, project=test_project, team_id="1", key=test_key
    ):
        m.get(self.url + "/api/0/", payload={"user": {"username": "foo"}})
        m.get(self.url + self.importer.organization_url, payload={"id": 1})
        m.get(self.url + self.importer.organization_users_url, payload=org_users or [])
        m.get(self.url + self.importer.projects_url, payload=[project])
        m.get(self.url + f"/api/0/projects/org/{project['slug']}/keys/", payload=[key])
        m.get(
            self.url + self.importer.teams_url,
            payload=[
                {
                    "id": team_id,
                    "slug": "team",
                    "projects": [project],
                }
            ],
        )
        m.get(self.url + "/api/0/teams/org/team/members/", payload=[])

    @mock_aiohttp()
    async def test_get_follows_pagination(self, m):
        first_url = self.url + self.importer.projects_url
        next_url = first_url + "?cursor=abc"
        m.get(
            first_url,
            payload=[{"id": "1"}],
            headers={"Link": f'<{next_url}>; rel="next"; results="true"; cursor="abc"'},
        )
        m.get(
            next_url,
            payload=[{"id": "2"}],
            headers={
                "Link": f'<{first_url}>; rel="next"; results="false"',
            },
        )

        data = await self.importer.get(first_url)

        self.assertEqual(data, [{"id": "1"}, {"id": "2"}])

    @mock_aiohttp()
    async def test_get_rebases_cross_origin_pagination(self, m):
        """A pagination link on another host is followed on ours, not theirs.

        Nothing may be requested from ``bad.example.com``: an unregistered URL
        raises, so registering only the rebased URL is the assertion that the
        auth token never left the origin the operator supplied.
        """
        first_url = self.url + self.importer.projects_url
        foreign_url = "https://bad.example.com" + self.importer.projects_url
        m.get(
            first_url,
            payload=[{"id": "1"}],
            headers={"Link": f'<{foreign_url}?cursor=abc>; rel="next"; results="true"'},
        )
        m.get(
            first_url + "?cursor=abc",
            payload=[{"id": "2"}],
            headers={"Link": f'<{first_url}>; rel="next"; results="false"'},
        )

        data = await self.importer.get(first_url)

        self.assertEqual(data, [{"id": "1"}, {"id": "2"}])

    @mock_aiohttp()
    async def test_get_follows_link_built_from_a_foreign_url_prefix(self, m):
        """Follow pagination from a server that names itself, not the request.

        This is the ``Link`` header shape a sentry 9.1.2 server emits, captured
        from a running instance with the host replaced. Two properties matter
        and neither is contrived: the link is absolute against whatever public
        address that server is *configured* with rather than the address the
        request arrived on, so it differs from ours whenever a proxy or a
        port-forward sits in between; and ``rel="previous"`` precedes
        ``rel="next"``, so a parser that inspects only the first link finds the
        wrong one. The path and query -- including the empty-query ``?&`` --
        have to survive being re-anchored for the cursor to still mean anything.
        """
        first_url = self.url + self.importer.projects_url
        cursor_query = "?&cursor=100:1:0"
        configured = "https://sentry.example.com" + self.importer.projects_url
        m.get(
            first_url,
            payload=[{"id": "1"}],
            headers={
                "Link": (
                    f'<{configured}?&cursor=100:-1:1>; rel="previous"; '
                    'results="false"; cursor="100:-1:1", '
                    f'<{configured}{cursor_query}>; rel="next"; '
                    'results="true"; cursor="100:1:0"'
                )
            },
        )
        m.get(
            first_url + cursor_query,
            payload=[{"id": "2"}],
            headers={
                "Link": (f'<{configured}{cursor_query}>; rel="next"; results="false"')
            },
        )

        data = await self.importer.get(first_url)

        self.assertEqual(data, [{"id": "1"}, {"id": "2"}])

    @mock_aiohttp()
    async def test_get_stops_when_the_next_link_repeats_a_page(self, m):
        """End the walk when a remote points back at a page already fetched.

        A server that always advertises a next link, flagging the last page only
        by pointing at itself, would otherwise be crawled until the page cap and
        fail an import that had in fact finished.
        """
        first_url = self.url + self.importer.projects_url
        second_url = first_url + "?cursor=abc"
        m.get(
            first_url,
            payload=[{"id": "1"}],
            headers={"Link": f'<{second_url}>; rel="next"'},
        )
        m.get(
            second_url,
            payload=[{"id": "2"}],
            headers={"Link": f'<{second_url}>; rel="next"'},
        )

        data = await self.importer.get(first_url)

        self.assertEqual(data, [{"id": "1"}, {"id": "2"}])

    @mock_aiohttp()
    async def test_get_rejects_a_non_list_page_mid_walk(self, m):
        """Don't silently discard collected pages if a later one isn't a list.

        Returning the odd page would drop everything gathered before it and hand
        back a shape the caller does not expect.
        """
        first_url = self.url + self.importer.projects_url
        second_url = first_url + "?cursor=abc"
        m.get(
            first_url,
            payload=[{"id": "1"}],
            headers={"Link": f'<{second_url}>; rel="next"; results="true"'},
        )
        m.get(second_url, payload={"detail": "gateway confused"})

        with self.assertRaises(ImporterException):
            await self.importer.get(first_url)

    @mock_aiohttp()
    async def test_get_stops_at_pagination_cap(self, m):
        first_url = self.url + self.importer.projects_url
        last_url = first_url
        for index in range(MAX_IMPORTER_PAGES):
            current_url = first_url if index == 0 else f"{first_url}?cursor={index}"
            next_url = f"{first_url}?cursor={index + 1}"
            last_url = next_url
            m.get(
                current_url,
                payload=[{"id": str(index)}],
                headers={"Link": f'<{next_url}>; rel="next"; results="true"'},
            )
        m.get(last_url, payload=[{"id": "not reached"}])

        with self.assertRaises(ImporterException):
            await self.importer.get(first_url)

    @mock_aiohttp()
    def test_import_command_creates_users(self, m):
        """Cover the management command's ``create_users`` branch.

        The command is the only caller that passes ``create_users=True``, and
        resolving an imported user needs a query. ``test_import_command`` never
        reached that code because it answers the members endpoint with an empty
        list, which returns before the loop, so a query issued there the wrong
        way for an async caller stayed invisible.
        """
        self.set_mocks(m, org_users=[test_org_user])

        call_command("import", self.url, self.auth_token, self.org_name)

        user = User.objects.get(email=test_org_user["email"])
        self.assertEqual(
            OrganizationUser.objects.get(email=test_org_user["email"]).user_id, user.pk
        )
        self.assertEqual(
            OrganizationUser.objects.get(email=test_org_user["email"]).role,
            OrganizationUserRole.OWNER,
        )

    @mock_aiohttp()
    def test_import_command(self, m):
        self.set_mocks(m, project=renamed_project)

        call_command("import", self.url, self.auth_token, self.org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                pk=renamed_project["id"],
                slug=renamed_project["slug"],
                teams__slug="team",
                projectkey__public_key=test_key["public"],
            ).exists()
        )

    def log_in(self):
        self.create_logged_in_user()
        self.organization.slug = self.org_name
        self.organization.save()

    def post_import(self):
        return self.client.post(
            reverse("api:importer"),
            {
                "url": self.url,
                "authToken": self.auth_token,
                "organizationSlug": self.org_name,
            },
            content_type="application/json",
        )

    @mock_aiohttp()
    def test_view(self, m):
        """Web imports don't copy keys; the project keeps the one it was created with."""
        self.log_in()
        self.set_mocks(m)
        res = self.post_import()
        self.assertEqual(res.status_code, 200)
        project = Project.objects.get(
            organization=self.organization,
            slug=test_project["slug"],
            teams__slug="team",
        )
        self.assertEqual(project.projectkey_set.count(), 1)
        self.assertFalse(
            project.projectkey_set.filter(public_key=test_key["public"]).exists()
        )

    @mock_aiohttp()
    def test_view_writes_only_to_the_importing_org(self, m):
        """Remote ids never select a row; rows are matched by slug in this org."""
        other_org = baker.make("organizations_ext.Organization")
        other_project = baker.make("projects.Project", organization=other_org)
        other_slug = other_project.slug
        other_team = baker.make("teams.Team", organization=other_org, slug="team")
        other_member = other_org.add_user(baker.make("users.User"))
        other_org.add_user(baker.make("users.User"))
        self.log_in()
        self.set_mocks(
            m,
            org_users=[{**test_org_user, "id": str(other_member.id)}],
            project={**test_project, "id": str(other_project.id)},
            team_id=str(other_team.id),
        )

        res = self.post_import()

        self.assertEqual(res.status_code, 200)
        for row in (other_project, other_team, other_member):
            row.refresh_from_db()
            self.assertEqual(row.organization_id, other_org.id)
        self.assertEqual(other_project.slug, other_slug)
        self.assertTrue(
            Project.objects.filter(
                organization=self.organization,
                slug=test_project["slug"],
                teams__organization=self.organization,
            ).exists()
        )
        self.assertTrue(
            OrganizationUser.objects.filter(
                organization=self.organization, email=test_org_user["email"]
            ).exists()
        )

    @mock_aiohttp()
    def test_view_keeps_the_remote_slug_when_it_differs_from_the_name(self, m):
        """A renamed project keeps its slug, so re-imports match it and teams link."""
        self.log_in()
        for _ in range(2):
            self.set_mocks(m, project=renamed_project)
            self.assertEqual(self.post_import().status_code, 200)

        imported = Project.objects.get(
            organization=self.organization, name=renamed_project["name"]
        )
        self.assertEqual(imported.slug, renamed_project["slug"])
        self.assertTrue(imported.teams.filter(slug="team").exists())

    @mock_aiohttp()
    def test_view_caps_roles_and_skips_existing_members(self, m):
        """An import can't grant above the importer's role, or edit a member."""
        self.log_in()
        self.org_user.role = OrganizationUserRole.MANAGER
        self.org_user.save()
        invite = baker.make(
            "organizations_ext.OrganizationUser",
            organization=self.organization,
            email="invited@example.com",
            role=OrganizationUserRole.MEMBER,
        )
        self.set_mocks(
            m,
            org_users=[
                {**test_org_user, "email": email}
                for email in ("new@example.com", invite.email, self.user.email)
            ],
        )

        res = self.post_import()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            OrganizationUser.objects.get(email="new@example.com").role,
            OrganizationUserRole.MANAGER,
        )
        invite.refresh_from_db()
        self.assertEqual(invite.role, OrganizationUserRole.MEMBER)
        self.org_user.refresh_from_db()
        self.assertEqual(self.org_user.role, OrganizationUserRole.MANAGER)
        self.assertFalse(
            OrganizationUser.objects.filter(email=self.user.email).exists()
        )

    @mock_aiohttp()
    def test_view_with_no_teams_or_no_projects(self, m):
        self.log_in()
        for projects in ([test_project], []):
            with self.subTest(projects=len(projects)):
                m.get(self.url + "/api/0/", payload={"user": {"username": "foo"}})
                m.get(self.url + self.importer.organization_users_url, payload=[])
                m.get(self.url + self.importer.projects_url, payload=projects)
                m.get(
                    self.url + "/api/0/projects/org/project/keys/", payload=[test_key]
                )
                m.get(self.url + self.importer.teams_url, payload=[])
                self.assertEqual(self.post_import().status_code, 200)
        self.assertTrue(
            Project.objects.filter(
                organization=self.organization, slug=test_project["slug"]
            ).exists()
        )

    @mock_aiohttp()
    def test_view_refuses_a_deleted_project_slug(self, m):
        """A deleted project still holds its slug; importing into it would vanish."""
        self.log_in()
        deleted = baker.make(
            "projects.Project", organization=self.organization, is_deleted=True
        )
        Project.objects.filter(pk=deleted.pk).update(slug=test_project["slug"])
        self.set_mocks(m)

        res = self.post_import()

        self.assertEqual(res.status_code, 400)
        self.assertIn(test_project["slug"], res.json()["detail"])
        self.assertFalse(
            deleted.projectkey_set.filter(public_key=test_key["public"]).exists()
        )

    @mock_aiohttp()
    async def test_invalid_org(self, m):
        """An import the caller may not perform never reaches the remote.

        The endpoint takes an auth token for another server, so who is refused
        matters less than when: the refusal has to land before anything is sent.
        Registering no response asserts that -- any outbound request would
        raise. Membership alone is not enough either; the view demands admin.
        """
        await sync_to_async(self.create_logged_in_user)()
        await self.async_client.aforce_login(self.user)
        url = reverse("api:importer")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": "foo",
        }
        # No such organization.
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 404)

        # The organization exists, but belongs to someone else.
        other_user = await baker.amake("users.User")
        other_org = await baker.amake("Organization", name="foo")
        await other_org.aadd_user(other_user)
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 404)

        # A member of it, but not an admin of it.
        await other_org.aadd_user(self.user)
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 404)

        self.assertEqual(m.requests, [])


@override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=False)
class ImportInValidationTests(SimpleTestCase):
    """The import source URL is org-admin-supplied; reject internal targets."""

    def _validate(self, url):
        return ImportIn.model_validate(
            {"url": url, "authToken": "t", "organizationSlug": "o"}
        )

    def test_rejects_metadata_url(self):
        with self.assertRaises(ValidationError):
            self._validate("http://169.254.169.254/latest/")

    def test_rejects_loopback_url(self):
        with self.assertRaises(ValidationError):
            self._validate("http://127.0.0.1/api/0/")

    def test_accepts_public_url(self):
        self._validate("https://8.8.8.8/")  # public literal, no raise

    @override_settings(GLITCHTIP_ALLOW_PRIVATE_IPS=True)
    def test_allow_private_setting_permits_internal(self):
        self._validate("http://127.0.0.1/")
