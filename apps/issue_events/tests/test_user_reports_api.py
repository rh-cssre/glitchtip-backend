from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase, GlitchTipTestCaseMixin


def get_issue_event_url(issue_id: int, event_id: str) -> str:
    return reverse(
        "api:get_issue_event", kwargs={"issue_id": issue_id, "event_id": event_id}
    )


def list_user_reports_url(issue_id: int) -> str:
    return reverse("api:list_user_reports", kwargs={"issue_id": issue_id})


class IssuesUserReportTestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()

        self.event = baker.make("issue_events.IssueEvent", issue__project=self.project)

        self.user_report = baker.make(
            "issue_events.UserReport",
            project=self.project,
            issue=self.event.issue,
            event_id=self.event.pk.hex,
        )

    def test_events_user_report(self):
        url = get_issue_event_url(self.event.issue_id, self.event.pk.hex)

        res = self.client.get(url)
        self.assertContains(res, self.user_report.email)
        self.assertContains(res, self.user_report.name)
        self.assertContains(res, self.user_report.comments)
        self.assertEqual(res.json()["userReport"]["eventID"], self.event.pk.hex)

    def test_issues_user_report_list(self):
        event2 = baker.make("issue_events.IssueEvent", issue__project=self.project)
        user_report2 = baker.make(
            "issue_events.UserReport",
            project=self.project,
            issue=event2.issue,
            event_id=event2.pk.hex,
        )
        url = list_user_reports_url(self.event.issue.id)
        res = self.client.get(url)
        self.assertContains(res, self.user_report.email)
        self.assertNotContains(res, user_report2.email)


class UserReportAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_org_team_project()
        self.set_client_credentials(self.auth_token.token)
        self.issue = baker.make("issue_events.Issue", project=self.project)
        self.user_report = baker.make(
            "issue_events.UserReport", project=self.project, issue=self.issue
        )
        self.list_url = list_user_reports_url(self.issue.id)

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)
