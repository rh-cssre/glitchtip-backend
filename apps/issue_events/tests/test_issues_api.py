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

    def test_project_issue_list(self):
        not_my_project = baker.make("projects.Project", organization=self.organization)
        not_my_issue = baker.make("issue_events.Issue", project=not_my_project)
        issue = baker.make("issue_events.Issue", project=self.project, short_id=1)
        baker.make("issue_events.IssueEvent", issue=issue)

        url = reverse(
            "api:list_project_issues",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        res = self.client.get(url)
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

    def test_tag_space(self):
        tag_name = "os.name"
        tag_value = "Linux Vista"
        event = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_name: tag_value, "foo": "bar"},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key__key=tag_name,
            tag_value__value=tag_value,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key__key="foo",
            tag_value__value="bar",
        )
        event2 = baker.make(
            "events.Event", issue__project=self.project, tags={tag_name: "BananaOS 7"}
        )

        res = self.client.get(
            self.list_url + f'?query={tag_name}:"Linux+Vista" foo:bar'
        )
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, event2.issue.title)

    def test_filter_by_tag(self):
        tag_browser = "browser.name"
        tag_value_firefox = "Firefox"
        tag_value_chrome = "Chrome"
        tag_value_cthulhu = "Cthulhu"
        tag_mythic_animal = "mythic_animal"

        key_browser = baker.make("issue_events.TagKey", key=tag_browser)
        key_mythic_animal = baker.make("issue_events.TagKey", key=tag_mythic_animal)
        value_firefox = baker.make("issue_events.TagValue", value=tag_value_firefox)
        value_chrome = baker.make("issue_events.TagValue", value=tag_value_chrome)
        value_cthulhu = baker.make("issue_events.TagValue", value=tag_value_cthulhu)

        event_only_firefox = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_only_firefox.issue,
            tag_key=key_browser,
            tag_value=value_firefox,
        )

        event_only_firefox2 = baker.make(
            "issue_events.IssueEvent",
            issue=event_only_firefox.issue,
            tags={tag_mythic_animal: tag_value_cthulhu},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_only_firefox2.issue,
            tag_key=key_mythic_animal,
            tag_value=value_cthulhu,
        )

        event_firefox_chrome = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_firefox_chrome.issue,
            tag_key=key_browser,
            tag_value=value_firefox,
        )

        event_firefox_chrome2 = baker.make(
            "issue_events.IssueEvent",
            issue=event_firefox_chrome.issue,
            tags={tag_browser: tag_value_chrome},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_firefox_chrome2.issue,
            tag_key=key_browser,
            tag_value=value_chrome,
        )

        event_no_tags = baker.make("events.Event", issue__project=self.project)

        event_browser_chrome_mythic_animal_firefox = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_mythic_animal: tag_value_firefox, tag_browser: tag_value_chrome},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_browser_chrome_mythic_animal_firefox.issue,
            tag_key=key_mythic_animal,
            tag_value=value_firefox,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event_browser_chrome_mythic_animal_firefox.issue,
            tag_key=key_browser,
            tag_value=value_chrome,
        )

        url = self.list_url
        res = self.client.get(url + f'?query={tag_browser}:"{tag_value_firefox}"')
        self.assertContains(res, event_only_firefox.issue.title)
        self.assertContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

        # Browser is Firefox AND Chrome
        res = self.client.get(
            url
            + f"?query={tag_browser}:{tag_value_firefox} {tag_browser}:{tag_value_chrome}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

        # Browser mythic_animal is Firefox
        res = self.client.get(url + f"?query={tag_mythic_animal}:{tag_value_firefox}")
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Chrome AND mythic_animal is Firefox
        res = self.client.get(
            url
            + f"?query={tag_browser}:{tag_value_chrome} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Firefox AND mythic_animal is Firefox
        res = self.client.get(
            url
            + f"?query={tag_browser}:{tag_value_firefox} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

    def test_filter_by_tag_distinct(self):
        tag_browser = "browser.name"
        tag_value = "Firefox"
        tag_value2 = "Chrome"

        key_browser = baker.make("issue_events.TagKey", key=tag_browser)
        value = baker.make("issue_events.TagValue", value=tag_value)
        value2 = baker.make("issue_events.TagValue", value=tag_value2)

        event = baker.make(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value},
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        baker.make(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value},
            _quantity=2,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        baker.make(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value},
            _quantity=5,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value2,
        )
        baker.make(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value2},
            _quantity=5,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value2,
        )

        res = self.client.get(self.list_url + f'?query={tag_browser}:"{tag_value}"')
        self.assertEqual(len(res.json()), 1)


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
