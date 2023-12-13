from django.shortcuts import reverse
from django.test import TestCase
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


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
        url = reverse(
            "api:get_issue_event",
            kwargs={"issue_id": self.event.issue_id, "event_id": self.event.pk.hex},
        )

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
        url = reverse(
            "api:list_user_reports", kwargs={"issue_id": self.event.issue.id}
        )
        res = self.client.get(url)
        self.assertContains(res, self.user_report.email)
        self.assertNotContains(res, user_report2.email)
