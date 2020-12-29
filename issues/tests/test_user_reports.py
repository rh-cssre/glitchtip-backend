from django.shortcuts import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class IssuesUserReportTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.event = baker.make("events.Event", issue__project=self.project)
        self.user_report = baker.make(
            "user_reports.UserReport",
            project=self.project,
            issue=self.event.issue,
            event_id=self.event.pk.hex,
        )

    def test_events_user_report(self):
        url = reverse(
            "project-events-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "pk": self.event.pk.hex,
            },
        )
        res = self.client.get(url)
        self.assertContains(res, self.user_report.email)
        self.assertContains(res, self.user_report.name)
        self.assertContains(res, self.user_report.comments)
        self.assertEqual(res.data["userReport"]["eventId"], self.event.pk.hex)

    def test_issues_list_user_report_count(self):
        url = reverse("issue-detail", kwargs={"pk": self.event.issue.pk})
        with self.assertNumQueries(6):
            res = self.client.get(url)
        self.assertEqual(res.data["userReportCount"], 1)

    def test_issues_user_report_list(self):
        event2 = baker.make("events.Event", issue__project=self.project)
        user_report2 = baker.make(
            "user_reports.UserReport",
            project=self.project,
            issue=event2.issue,
            event_id=event2.pk.hex,
        )
        url = reverse(
            "issue-user-reports-list", kwargs={"issue_pk": self.event.issue.pk}
        )
        res = self.client.get(url)
        self.assertContains(res, self.user_report.email)
        self.assertNotContains(res, user_report2.email)
