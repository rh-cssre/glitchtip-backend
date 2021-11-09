# pylint: disable=attribute-defined-outside-init,invalid-name
from model_bakery import baker
from rest_framework.test import APITestCase

from organizations_ext.models import OrganizationUserRole


class GlitchTipTestCase(APITestCase):
    def create_user_and_project(self):
        self.user = baker.make("users.user")
        self.organization = baker.make(
            "organizations_ext.Organization", scrub_ip_addresses=False
        )
        self.org_user = self.organization.add_user(
            self.user, OrganizationUserRole.ADMIN
        )
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.client.force_login(self.user)


class APIPermissionTestCase(APITestCase):
    """ Base class for testing viewsets with permissions """

    def create_user_org(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.org_user = self.organization.add_user(self.user)
        self.auth_token = baker.make("api_tokens.APIToken", user=self.user)

    def set_client_credentials(self, token: str):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token)

    def set_user_role(self, role: OrganizationUserRole):
        self.org_user.role = role
        self.org_user.save(update_fields=["role"])

    def assertGetReqStatusCode(self, url: str, status_code: int, msg=None):
        """ Make GET request to url and check status code """
        res = self.client.get(url)
        self.assertEqual(res.status_code, status_code, msg)

    def assertPostReqStatusCode(self, url: str, data, status_code: int, msg=None):
        """ Make POST request to url and check status code """
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, status_code, msg)

    def assertPutReqStatusCode(self, url: str, data, status_code: int, msg=None):
        """ Make PUT request to url and check status code """
        res = self.client.put(url, data)
        self.assertEqual(res.status_code, status_code, msg)

    def assertDeleteReqStatusCode(self, url: str, status_code: int, msg=None):
        """ Make DELETE request to url and check status code """
        res = self.client.delete(url)
        self.assertEqual(res.status_code, status_code, msg)
