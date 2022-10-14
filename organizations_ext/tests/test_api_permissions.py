from django.urls import reverse
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class OrganizationAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.list_url = reverse("organization-list")
        self.detail_url = reverse("organization-detail", args=[self.organization.slug])

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("org:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("org:read")
        self.assertGetReqStatusCode(self.detail_url, 200)

    def test_create(self):
        self.auth_token.add_permission("org:read")
        data = {"name": "new org"}
        self.assertPostReqStatusCode(self.list_url, data, 403)
        self.auth_token.add_permission("org:write")
        self.assertPostReqStatusCode(self.list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["org:read", "org:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("org:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 403)
        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("org:read")
        data = {"name": "new name"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.auth_token.add_permission("org:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)

    def test_user_update(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        data = {"name": "new name"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.set_user_role(OrganizationUserRole.MANAGER)
        self.assertPutReqStatusCode(self.detail_url, data, 200)


class OrganizationMemberAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.list_url = reverse(
            "organization-members-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.detail_url = reverse(
            "organization-members-detail",
            kwargs={
                "organization_slug": self.organization.slug,
                "pk": self.org_user.pk,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("member:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("member:read")
        self.assertGetReqStatusCode(self.detail_url, 200)

    def test_create(self):
        self.auth_token.add_permission("member:read")
        data = {"email": "lol@example.com", "role": "member"}
        self.assertPostReqStatusCode(self.list_url, data, 403)
        self.auth_token.add_permission("member:write")
        self.assertPostReqStatusCode(self.list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["member:read", "member:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("member:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 403)
        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("member:read")
        data = {"email": "lol@example.com", "role": "member"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.auth_token.add_permission("member:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)

    def test_teams_add(self):
        self.team = baker.make("teams.Team", organization=self.organization)
        url = self.detail_url + "teams/" + self.team.slug + "/"
        data = {}
        self.assertPostReqStatusCode(url, data, 403)
        self.auth_token.add_permissions(["org:read", "org:write"])
        self.assertPostReqStatusCode(url, data, 201)

    def test_teams_remove(self):
        self.team = baker.make("teams.Team", organization=self.organization)
        url = self.detail_url + "teams/" + self.team.slug + "/"
        self.assertDeleteReqStatusCode(url, 403)
        self.auth_token.add_permissions(["org:read", "org:write"])
        self.assertDeleteReqStatusCode(url, 200)

