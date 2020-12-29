from django.urls import reverse
from model_bakery import baker
from organizations_ext.models import OrganizationUserRole
from glitchtip.test_utils.test_case import APIPermissionTestCase


class IssueAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.issue = baker.make("issues.Issue", project=self.project)
        self.list_url = reverse("issue-list")
        self.organization_list_url = reverse(
            "organization-issues-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        self.project_list_url = reverse(
            "project-issues-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )
        self.detail_url = reverse("issue-detail", kwargs={"pk": self.issue.pk})
        self.organization_detail_url = reverse(
            "organization-issues-detail",
            kwargs={"organization_slug": self.organization.slug, "pk": self.issue.pk},
        )
        self.project_detail_url = reverse(
            "project-issues-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "pk": self.issue.pk,
            },
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.assertGetReqStatusCode(self.organization_list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.assertGetReqStatusCode(self.organization_detail_url, 403)
        self.assertGetReqStatusCode(self.project_detail_url, 403)

        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.detail_url, 200)
        self.assertGetReqStatusCode(self.organization_detail_url, 200)
        self.assertGetReqStatusCode(self.project_detail_url, 200)

    def test_create(self):
        data = {"not": "supported"}
        self.auth_token.add_permission("event:admin")
        self.assertPostReqStatusCode(self.list_url, data, 405)

    def test_destroy(self):
        self.auth_token.add_permissions(["event:read", "event:write"])
        self.assertDeleteReqStatusCode(self.detail_url, 403)

        self.auth_token.add_permission("event:admin")
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_user_destroy(self):
        self.client.force_login(self.user)
        self.set_user_role(OrganizationUserRole.MEMBER)
        self.assertDeleteReqStatusCode(self.detail_url, 204)

    def test_update(self):
        self.auth_token.add_permission("event:read")
        data = {"status": "resolved"}
        self.assertPutReqStatusCode(self.detail_url, data, 403)
        self.assertPutReqStatusCode(self.organization_detail_url, data, 403)
        self.assertPutReqStatusCode(self.project_detail_url, data, 403)

        self.auth_token.add_permission("event:write")
        self.assertPutReqStatusCode(self.detail_url, data, 200)
        self.assertPutReqStatusCode(self.organization_detail_url, data, 200)
        self.assertPutReqStatusCode(self.project_detail_url, data, 200)


class EventAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.event = baker.make("events.Event", issue__project=self.project)
        self.list_url = reverse(
            "issue-events-list", kwargs={"issue_pk": self.event.issue.pk}
        )
        self.project_list_url = reverse(
            "project-events-list",
            kwargs={"project_pk": self.organization.slug + "/" + self.project.slug},
        )
        self.detail_url = reverse(
            "issue-events-detail",
            kwargs={"issue_pk": self.event.issue.pk, "pk": self.event.pk},
        )
        self.project_detail_url = reverse(
            "project-events-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "pk": self.event.pk,
            },
        )
        self.latest_detail_url = self.list_url + "latest/"

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.assertGetReqStatusCode(self.project_list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)
        self.assertGetReqStatusCode(self.project_list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.assertGetReqStatusCode(self.project_detail_url, 403)
        self.assertGetReqStatusCode(self.latest_detail_url, 403)

        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.detail_url, 200)
        self.assertGetReqStatusCode(self.project_detail_url, 200)
        self.assertGetReqStatusCode(self.latest_detail_url, 200)

    def test_event_json_view(self):
        url = reverse(
            "event_json",
            kwargs={
                "org": self.organization.slug,
                "issue": self.event.issue.pk,
                "event": self.event.pk,
            },
        )
        self.assertGetReqStatusCode(url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(url, 200)
