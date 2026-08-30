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

    def set_mocks(self, m, org_users=None):
        m.get(self.url + "/api/0/", payload={"user": {"username": "foo"}})
        m.get(self.url + self.importer.organization_url, payload={"id": 1})
        m.get(self.url + self.importer.organization_users_url, payload=org_users or [])
        m.get(self.url + self.importer.projects_url, payload=[test_project])
        m.get(self.url + "/api/0/projects/org/project/keys/", payload=[test_key])
        m.get(
            self.url + self.importer.teams_url,
            payload=[
                {
                    "id": "1",
                    "slug": "team",
                    "projects": [test_project],
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
        self.set_mocks(m)

        call_command("import", self.url, self.auth_token, self.org_name)
        self.assertTrue(Team.objects.filter(slug="team").exists())
        self.assertTrue(
            Project.objects.filter(
                slug=test_project["slug"],
                teams__slug="team",
                projectkey__public_key=test_key["public"],
            ).exists()
        )

    @mock_aiohttp()
    def test_view(self, m):
        self.create_logged_in_user()
        self.organization.slug = self.org_name
        self.organization.save()
        self.set_mocks(m)
        url = reverse("api:importer")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": self.org_name,
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Team.objects.filter(slug="team").exists())

    @mock_aiohttp()
    async def test_invalid_org(self, m):
        await sync_to_async(self.create_logged_in_user)()
        await self.async_client.aforce_login(self.user)
        url = reverse("api:importer")
        data = {
            "url": self.url,
            "authToken": self.auth_token,
            "organizationSlug": "foo",
        }
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)
        other_user = await baker.amake("users.User")
        other_org = await baker.amake("Organization", name="foo")
        await other_org.aadd_user(other_user)
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)
        await other_org.aadd_user(self.user)
        m.get(self.url + "api/0/", payload={"user": {"username": "foo"}})
        res = await self.async_client.post(url, data)
        self.assertEqual(res.status_code, 400)


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
