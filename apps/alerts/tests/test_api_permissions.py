from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class AlertsAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.teams.add(self.team)
        self.alert = baker.make("alerts.ProjectAlert", project=self.project)
        self.list_url = reverse(
            "api:list_project_alerts", args=[self.organization.slug, self.project.slug]
        )
        self.detail_url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, self.alert.id],
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)

        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_create(self):
        self.auth_token.add_permission("project:read")
        data = {"timespan_minutes": 1, "quantity": 1}
        self.assertPostReqStatusCode(self.list_url, data, 403)

        self.auth_token.add_permission("project:write")
        self.assertPostReqStatusCode(self.list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["project:read", "project:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.auth_token.add_permission("project:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.set_client_credentials(None)
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 404)

        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("project:read")
        data = {"timespan_minutes": 1, "quantity": 1}
        self.assertPutReqStatusCode(self.detail_url, data, 403)

        self.auth_token.add_permission("project:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)
