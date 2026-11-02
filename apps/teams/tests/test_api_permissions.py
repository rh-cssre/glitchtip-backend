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
            kwargs={"organization_id_or_slug": self.organization.slug},
        )
        self.project_list_url = reverse(
            "api:list_project_teams",
            kwargs={
                "organization_id_or_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        self.detail_url = reverse(
            "api:get_team",
            kwargs={
                "organization_id_or_slug": self.organization.slug,
                "team_slug": self.team.slug,
            },
        )
        self.create_team_url = reverse(
            "api:create_team",
            kwargs={
                "organization_id_or_slug": self.organization.slug,
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
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.set_user_role(OrganizationUserRole.OWNER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("team:read")
        data = {"slug": "new-slug"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)

        self.auth_token.add_permission("team:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)

    def member_team_url(self, member_id="me", organization_id_or_slug=None):
        return reverse(
            "api:add_member_to_team",
            args=[
                organization_id_or_slug or self.organization.slug,
                member_id,
                self.team.slug,
            ],
        )

    def test_member_to_team_token_ceiling(self):
        """Tokens need team:write or team:admin, as on master."""
        self.set_user_role(OrganizationUserRole.MANAGER)
        url = self.member_team_url()
        self.auth_token.add_permission("team:read")
        self.assertPostReqStatusCode(url, {}, 403)
        self.assertDeleteReqStatusCode(url, 403)

        self.auth_token.add_permission("team:write")
        self.assertPostReqStatusCode(url, {}, 201)
        self.assertDeleteReqStatusCode(url, 200)

    def test_member_token_joins_open_team(self):
        """The gate admits every role, so a member's token joins and leaves an
        open-membership team; adding someone else is still refused."""
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.auth_token.add_permissions(["team:read", "team:write"])
        url = self.member_team_url()
        self.assertPostReqStatusCode(url, {}, 201)
        self.assertTrue(self.team.members.filter(id=self.org_user.id).exists())
        self.assertDeleteReqStatusCode(url, 200)
        self.assertFalse(self.team.members.exists())

        other = self.organization.add_user(baker.make("users.User"))
        self.assertPostReqStatusCode(self.member_team_url(other.id), {}, 403)

    def test_member_token_with_write_scope_only(self):
        """Differs from master, which let this token join: the gate maps
        team:write alone to admin and up."""
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.auth_token.add_permission("team:write")
        self.assertPostReqStatusCode(self.member_team_url(), {}, 403)

    def test_member_to_team_by_org_id(self):
        self.set_user_role(OrganizationUserRole.MANAGER)
        self.auth_token.add_permission("team:write")
        url = self.member_team_url(organization_id_or_slug=self.organization.id)
        self.assertPostReqStatusCode(url, {}, 201)
        self.assertDeleteReqStatusCode(url, 200)

    def test_member_to_team_non_member(self):
        stranger_org = baker.make("organizations_ext.Organization")
        stranger_team = baker.make("teams.Team", organization=stranger_org)
        # A real member of that org, so only the gate can refuse
        stranger = stranger_org.add_user(baker.make("users.User"))
        self.set_user_role(OrganizationUserRole.OWNER)
        self.auth_token.add_permission("team:write")
        for org in [stranger_org.slug, stranger_org.id]:
            with self.subTest(org=org):
                url = reverse(
                    "api:add_member_to_team",
                    args=[org, stranger.id, stranger_team.slug],
                )
                self.assertPostReqStatusCode(url, {}, 404)
        self.assertFalse(stranger_team.members.exists())
