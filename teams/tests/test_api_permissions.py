from django.urls import reverse
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class TeamAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.list_url = reverse("team-list")
        self.organization_list_url = reverse(
            "organization-teams-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.project_list_url = reverse(
            "project-teams-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )
        self.detail_url = reverse(
            "team-detail", kwargs={"pk": self.organization.slug + "/" + self.team.slug}
        )
        self.organization_detail_url = reverse(
            "organization-teams-detail",
            kwargs={"organization_slug": self.organization.slug, "pk": self.team.pk},
        )
        self.project_detail_url = reverse(
            "project-teams-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "pk": self.team.pk,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.assertGetReqStatusCode(self.organization_list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("team:read")
        self.assertGetReqStatusCode(self.list_url, 200)
        self.assertGetReqStatusCode(self.organization_list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.assertGetReqStatusCode(self.organization_detail_url, 403)
        self.assertGetReqStatusCode(self.project_detail_url, 403)

        self.auth_token.add_permission("team:read")
        self.assertGetReqStatusCode(self.detail_url, 200)
        self.assertGetReqStatusCode(self.organization_detail_url, 200)
        # ProjectTeamViewSet does not allow GET
        self.assertGetReqStatusCode(self.project_detail_url, 405)

    def test_create(self):
        self.auth_token.add_permission("team:read")
        data = {"slug": "new-team"}
        self.assertPostReqStatusCode(self.list_url, data, 403)
        self.assertPostReqStatusCode(self.organization_list_url, data, 403)
        self.assertPostReqStatusCode(self.project_list_url, data, 403)

        self.auth_token.add_permission("team:write")
        # Specifying organization from url slug is required
        self.assertPostReqStatusCode(self.list_url, data, 400)
        self.assertPostReqStatusCode(self.organization_list_url, data, 201)
        self.assertPostReqStatusCode(self.project_list_url, data, 400)
        data = {"slug": "new-team2"}
        self.assertPostReqStatusCode(self.project_list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["team:read", "team:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.auth_token.add_permission("team:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("team:read")
        data = {"slug": "new-slug"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.assertPutReqStatusCode(self.organization_detail_url, data, 403)

        self.auth_token.add_permission("team:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)
        self.assertPutReqStatusCode(self.organization_detail_url, data, 200)
