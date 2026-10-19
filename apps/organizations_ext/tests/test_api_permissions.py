from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class OrganizationAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.list_url = reverse("api:list_organizations")
        self.detail_url = reverse("api:get_organization", args=[self.organization.slug])

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
        self.set_client_credentials(None)
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
        user2 = baker.make("users.user")
        self.organization.add_user(user2, OrganizationUserRole.MANAGER)
        self.set_client_credentials(None)
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        data = {"name": "new name"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.set_user_role(OrganizationUserRole.MANAGER)
        self.assertPutReqStatusCode(self.detail_url, data, 200)


class OrganizationMemberAPIPermissionTests(APIPermissionTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user_org(cls)
        # Change owner to avoid restrictions on org owners
        # deleting their own organization
        new_user = baker.make("users.User")
        new_owner = cls.organization.add_user(new_user)
        cls.organization.change_owner(new_owner)
        cls.list_url = reverse(
            "api:list_organization_members", args=[cls.organization.slug]
        )
        cls.detail_url = reverse(
            "api:get_organization_member",
            args=[cls.organization.slug, cls.org_user.pk],
        )

    def setUp(self):
        self.set_client_credentials(self.auth_token.token)

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
        data = {"email": "lol@example.com", "orgRole": "member", "teams": []}
        self.assertPostReqStatusCode(self.list_url, data, 403)
        self.auth_token.add_permission("member:write")
        self.assertPostReqStatusCode(self.list_url, data, 201)

    def test_destroy(self):
        self.auth_token.add_permissions(["member:read", "member:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("member:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_member_token_removes_itself(self):
        """has_permission demands member:admin of the token, and the gate
        admits every role, so a member's token may remove its own user."""
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.auth_token.add_permissions(["member:read", "member:admin"])
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_member_token_removes_others_by_role(self):
        other = self.organization.add_user(baker.make("users.User"))
        url = reverse(
            "api:get_organization_member", args=[self.organization.slug, other.pk]
        )
        self.auth_token.add_permissions(["member:read", "member:admin"])
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(url, 403)
        self.set_user_role(OrganizationUserRole.MANAGER)
        self.assertDeleteReqStatusCode(url, 204)

    def test_member_token_with_admin_scope_only(self):
        """Differs from master, which let this token remove its own user: the
        gate maps member:admin alone to manager and up."""
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.auth_token.add_permission("member:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 403)

    def test_destroy_by_org_id(self):
        other = self.organization.add_user(baker.make("users.User"))
        self.set_user_role(OrganizationUserRole.MANAGER)
        self.auth_token.add_permission("member:admin")
        url = reverse(
            "api:get_organization_member", args=[self.organization.id, other.pk]
        )
        self.assertDeleteReqStatusCode(url, 204)

    def test_destroy_non_member(self):
        stranger_org = baker.make("organizations_ext.Organization")
        stranger = stranger_org.add_user(baker.make("users.User"))
        stranger_org.add_user(baker.make("users.User"))  # not the last member
        self.auth_token.add_permission("member:admin")
        for org in [stranger_org.slug, stranger_org.id]:
            with self.subTest(org=org):
                url = reverse("api:get_organization_member", args=[org, stranger.pk])
                self.assertDeleteReqStatusCode(url, 404)
        self.assertTrue(stranger_org.users.filter(id=stranger.user_id).exists())

    def test_user_destroy(self):
        another_user = baker.make("users.user")
        another_org_user = self.organization.add_user(another_user)
        url = reverse(
            "api:get_organization_member",
            args=[self.organization.slug, another_org_user.pk],
        )
        self.set_client_credentials(None)
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(url, 403)
        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(url, 204)

    def test_invite_destroy(self):
        invitee = baker.make(
            "organizations_ext.OrganizationUser", organization=self.organization
        )
        url = reverse(
            "api:get_organization_member",
            args=[self.organization.slug, invitee.pk],
        )
        self.set_client_credentials(None)
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(url, 403)
        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(url, 204)

    def test_update(self):
        baker.make(
            "organizations_ext.OrganizationUser",
            role=OrganizationUserRole.OWNER,
            organization=self.organization,
        )  # Ensure alternative owner exists
        self.auth_token.add_permission("member:read")
        data = {"email": "lol@example.com", "orgRole": "member"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.auth_token.add_permission("member:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)
