from django.shortcuts import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class IssuesUserReportTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_events_user_report(self):
        event = baker.make("issues.Event", issue__project=self.project)
        user_report = baker.make(
            "user_reports.UserReport",
            project=self.project,
            issue=event.issue,
            event_id=event.pk.hex,
        )
        url = reverse(
            "project-events-detail",
            kwargs={
                "project_pk": self.organization.slug + "/" + self.project.slug,
                "pk": event.pk.hex,
            },
        )
        res = self.client.get(url)
        self.assertContains(res, user_report.email)
        self.assertContains(res, user_report.name)
        self.assertContains(res, user_report.comments)
        self.assertEqual(res.data["userReport"]["eventId"], event.pk.hex)
