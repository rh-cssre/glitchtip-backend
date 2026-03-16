# pylint: disable=attribute-defined-outside-init,invalid-name
from django.test import TestCase
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization


class GlitchTestCase(TestCase):
    """
    Usage:

    class FooTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.url = "something that doesn't change between tests"
    def setUp(self):
        self.client.force_login(self.user)  # Must run every test
    """

    organization: Organization | None = None

    @classmethod
    def create_project(cls):
        """Create project, dsn, and organization"""
        cls.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        cls.projectkey = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization

    @classmethod
    def create_user(cls):
        """
        Create user and joins them to organization with a team
        If organization does not exist, create it
        """
        if not cls.organization:
            cls.create_project()
        cls.user = baker.make("users.user")
        cls.org_user = cls.organization.add_user(cls.user, OrganizationUserRole.ADMIN)
        cls.team = baker.make("teams.Team", organization=cls.organization)
        cls.team.members.add(cls.org_user)
        cls.project.teams.add(cls.team)


class GlitchTipTestCaseMixin:
    organization: Organization | None = None

    def create_project(self):
        """Create project, dsn, and organization"""
        self.project = baker.make(
            "projects.Project", organization__scrub_ip_addresses=False
        )
        self.projectkey = self.project.projectkey_set.first()
        self.organization = self.project.organization

    def create_logged_in_user(self):
        """
        Create user and joins them to organization with a team
        If organization does not exist, create it
        """
        if not self.organization:
            self.create_project()
        self.user = baker.make("users.user")
        self.client.force_login(self.user)
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)


class GlitchTipTestCase(GlitchTipTestCaseMixin, TestCase):
    """Use GlitchTestCase instead."""

    def create_user_and_project(self):
        self.create_logged_in_user()


class APIPermissionTestCase(TestCase):
    """Base class for testing views with permissions"""

    token: str | None = None

    def create_user_org(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)
        self.auth_token = baker.make("api_tokens.APIToken", user=self.user)

    def create_org_team_project(self):
        self.create_user_org()
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)

    def get_headers(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def set_client_credentials(self, token: str):
        self.token = token

    def set_user_role(self, role: OrganizationUserRole):
        self.org_user.role = role
        self.org_user.save(update_fields=["role"])

    def assertGetReqStatusCode(self, url: str, status_code: int, msg=None):
        """Make GET request to url and check status code"""
        res = self.client.get(url, **self.get_headers())
        self.assertEqual(res.status_code, status_code, msg)

    def assertPostReqStatusCode(self, url: str, data, status_code: int, msg=None):
        """Make POST request to url and check status code"""
        res = self.client.post(
            url, data, content_type="application/json", **self.get_headers()
        )
        self.assertEqual(res.status_code, status_code, msg)

    def assertPutReqStatusCode(self, url: str, data, status_code: int, msg=None):
        """Make PUT request to url and check status code"""
        res = self.client.put(
            url, data, content_type="application/json", **self.get_headers()
        )
        self.assertEqual(res.status_code, status_code, msg)

    def assertDeleteReqStatusCode(self, url: str, status_code: int, msg=None):
        """Make DELETE request to url and check status code"""
        res = self.client.delete(url, **self.get_headers())
        self.assertEqual(res.status_code, status_code, msg)
