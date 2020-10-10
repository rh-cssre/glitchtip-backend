from django.urls import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import APIPermissionTestCase


class UserReportAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.org_user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.issue = baker.make("issues.Issue", project=self.project)
        self.user_report = baker.make(
            "user_reports.UserReport", project=self.project, issue=self.issue
        )
        self.list_url = reverse(
            "issue-user-reports-list", kwargs={"issue_pk": self.issue.pk}
        )
        self.detail_url = reverse(
            "issue-user-reports-detail",
            kwargs={"issue_pk": self.issue.pk, "pk": self.user_report.pk},
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)

    def test_retrieve(self):
        self.assertGetReqStatusCode(self.detail_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.detail_url, 200)
