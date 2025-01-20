from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class TeamAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.list_url = reverse(
            "api:list_teams",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.project_list_url = reverse(
            "api:list_project_teams",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        self.detail_url = reverse(
            "api:get_team",
            kwargs={
                "organization_slug": self.organization.slug,
                "team_slug": self.team.slug,
            },
        )
        self.create_team_url = reverse(
            "api:create_team",
            kwargs={
                "organization_slug": self.organization.slug,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("team:read")
        self.assertGetReqStatusCode(self.list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("team:read")
        self.assertGetReqStatusCode(self.detail_url, 200)

    def test_create(self):
        self.auth_token.add_permission("team:read")
        data = {"slug": "new-team"}
        self.assertPostReqStatusCode(self.create_team_url, data, 403)

        self.auth_token.add_permission("team:write")
        self.assertPostReqStatusCode(self.create_team_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["team:read", "team:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.auth_token.add_permission("team:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.set_client_credentials(None)
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 404)

        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("team:read")
        data = {"slug": "new-slug"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)

        self.auth_token.add_permission("team:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)
