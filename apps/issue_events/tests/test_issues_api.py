from django.contrib.postgres.search import SearchVector
from django.db.models import Value
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase, GlitchTipTestCaseMixin


class IssueEventAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()
        self.list_url = reverse(
            "api:list_issues", kwargs={"organization_slug": self.organization.slug}
        )

    def test_retrieve(self):
        issue = baker.make("issue_events.Issue", project=self.project, short_id=1)
        event = baker.make("issue_events.IssueEvent", issue=issue)
        baker.make(
            "issue_events.UserReport",
            project=self.project,
            issue=issue,
            event_id=event.pk.hex,
            _quantity=1,
        )
        baker.make("issue_events.Comment", issue=issue, _quantity=3)
        url = reverse(
            "api:get_issue",
            kwargs={"issue_id": issue.id},
        )

        res = self.client.get(url)
        data = res.json()

        self.assertEqual(
            data.get("shortId"), f"{self.project.slug.upper()}-{issue.short_id}"
        )
        self.assertEqual(data.get("count"), str(issue.count))
        self.assertEqual(data.get("userReportCount"), 1)
        self.assertEqual(data.get("numComments"), 3)

    def test_list(self):
        res = self.client.get(self.list_url)
        self.assertEqual(res.status_code, 200)

        not_my_issue = baker.make("issue_events.Issue")
        issue = baker.make("issue_events.Issue", project=self.project, short_id=1)
        baker.make("issue_events.IssueEvent", issue=issue)
        res = self.client.get(self.list_url)
        self.assertContains(res, issue.title)
        self.assertNotContains(res, not_my_issue.title)
        self.assertEqual(len(res.json()), 1)

    def test_filter_by_date(self):
        """
        A user should be able to filter by start and end datetimes.
        In the future, this should filter events, not first_seen.
        """
        issue1 = baker.make(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(1999, 1, 1)),
            project=self.project,
        )
        issue2 = baker.make(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(2010, 1, 1)),
            project=self.project,
        )
        issue3 = baker.make(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(2020, 1, 1)),
            project=self.project,
        )
        res = self.client.get(
            self.list_url
            + "?start=2000-01-01T05:00:00.000Z&end=2019-01-01T05:00:00.000Z"
        )
        self.assertContains(res, issue2.title)
        self.assertNotContains(res, issue1.title)
        self.assertNotContains(res, issue3.title)

    def test_sort(self):
        issue1 = baker.make("issue_events.Issue", project=self.project)
        issue2 = baker.make("issue_events.Issue", project=self.project, count=2)
        issue3 = baker.make("issue_events.Issue", project=self.project)

        res = self.client.get(self.list_url)
        self.assertEqual(res.json()[0]["id"], str(issue3.id))

        res = self.client.get(self.list_url + "?sort=-count")
        self.assertEqual(res.json()[0]["id"], str(issue2.id))

        res = self.client.get(self.list_url + "?sort=priority")
        self.assertEqual(res.json()[0]["id"], str(issue1.id))

        res = self.client.get(self.list_url + "?sort=-priority")
        self.assertEqual(res.json()[0]["id"], str(issue2.id))

    def test_search(self):
        issue = baker.make(
            "issue_events.Issue",
            project=self.project,
            search_vector=SearchVector(Value("apple sauce")),
        )
        other_issue = baker.make("issue_events.Issue", project=self.project)

        res = self.client.get(self.list_url + "?query=is:unresolved apple+sauce")
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        self.assertNotContains(res, "matchingEventId")
        self.assertNotIn("X-Sentry-Direct-Hit", res.headers)


class IssueEventAPIPermissionTestCase(APIPermissionTestCase):
    def setUp(self):
        self.create_org_team_project()
        self.set_client_credentials(self.auth_token.token)
        self.issue = baker.make("issues.Issue", project=self.project)

        self.list_url = reverse(
            "api:list_issues", kwargs={"organization_slug": self.organization.slug}
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)
