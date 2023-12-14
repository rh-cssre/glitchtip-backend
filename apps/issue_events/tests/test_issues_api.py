from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


class IssueEventAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()

    def test_retrieve(self):
        issue = baker.make("issue_events.Issue", project=self.project, short_id=1)
        event = baker.make("issue_events.IssueEvent", issue=issue)
        issue_stats = baker.make("issue_events.IssueStats", issue=issue)
        baker.make(
            "issue_events.UserReport",
            project=self.project,
            issue=issue,
            event_id=event.pk.hex,
            _quantity=1
        )
        baker.make(
            "issue_events.Comment",
            issue=issue,
            _quantity=3
        )
        url = reverse(
            "api:get_issue",
            kwargs={ "issue_id": issue.id },
        )

        res = self.client.get(url)
        data = res.json()
        
        self.assertEqual(data.get("shortId"), f'{self.project.slug.upper()}-{issue.short_id}')
        self.assertEqual(data.get("count"), str(issue_stats.count))
        self.assertEqual(data.get("userReportCount"), 1)
        self.assertEqual(data.get("numComments"), 3)
