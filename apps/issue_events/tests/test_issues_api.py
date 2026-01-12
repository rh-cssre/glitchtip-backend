import datetime
import logging
from timeit import default_timer as timer

from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db.models import F, Value
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.event_ingest.model_functions import PipeConcat
from glitchtip.test_utils.test_case import (
    APIPermissionTestCase,
    GlitchTestCase,
)

from ..constants import EventStatus, LogLevel
from ..models import Issue

logger = logging.getLogger(__name__)


def get_issue_url(issue_id: int) -> str:
    return reverse("api:get_issue", kwargs={"issue_id": issue_id})


def get_organization_issue_url(organization_slug: str, issue_id: int) -> str:
    return reverse(
        "api:update_organization_issue",
        kwargs={"organization_slug": organization_slug, "issue_id": issue_id},
    )


class IssueAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.list_url = reverse(
            "api:list_issues", kwargs={"organization_slug": cls.organization.slug}
        )

    def setUp(self):
        self.client.force_login(self.user)

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
        expected_permalink = (
            f"{settings.GLITCHTIP_URL.geturl()}/{issue.project.slug}/issues/{issue.id}"
        )
        self.assertEqual(data.get("permalink"), expected_permalink)

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

    def test_priority_environment(self):
        baker.make("issue_events.Issue", project=self.project)
        res = self.client.get(self.list_url + "?sort=-priority&environment=env")
        self.assertEqual(res.status_code, 200)

    def test_search(self):
        issue = baker.make(
            "issue_events.Issue",
            project=self.project,
            search_vector=SearchVector(Value("apple sauce")),
        )
        event = baker.make("issue_events.IssueEvent", issue=issue)
        other_issue = baker.make("issue_events.Issue", project=self.project)

        res = self.client.get(self.list_url + "?query=is:unresolved apple+sauce")
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        # Not sure how to do this in Ninja without always removing None field values
        # self.assertNotContains(res, "matchingEventId")
        self.assertNotIn("X-Sentry-Direct-Hit", res.headers)

        res = self.client.get(self.list_url + "?query=is:unresolved apple sauce")
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)

        res = self.client.get(self.list_url + '?query=is:unresolved "apple sauce"')
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)

        res = self.client.get(self.list_url + "?query=" + event.id.hex)
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        self.assertContains(res, "matchingEventId")
        self.assertContains(res, event.id.hex)
        self.assertEqual(res.headers.get("X-Sentry-Direct-Hit"), "1")

        event3 = baker.make(
            "issue_events.IssueEvent", issue=issue, data={"name": "plum sauce"}
        )
        Issue.objects.filter(id=issue.id).update(
            search_vector=SearchVector(
                PipeConcat(F("search_vector"), SearchVector(Value(event3.data["name"])))
            )
        )
        issue.search_vector = SearchVector(Value("apple sauce plum "))
        res = self.client.get(self.list_url + '?query=is:unresolved "plum sauce"')
        self.assertContains(res, event3.issue.title)
        res = self.client.get(self.list_url + '?query=is:unresolved "apple sauce"')
        self.assertContains(res, event.issue.title)

    def test_search_wildcard(self):
        issue_str = "The foo want to the bar"
        issue = baker.make(
            "issue_events.Issue",
            project=self.project,
            title=issue_str,
            search_vector=SearchVector(Value(issue_str)),
        )
        res = self.client.get(self.list_url + "?query=is:unresolved f*o")
        self.assertContains(res, issue.title)
        res = self.client.get(self.list_url + "?query=is:unresolved f*x")
        self.assertNotContains(res, issue.title)

    def test_list_relative_datetime_filter(self):
        now = timezone.now()
        last_minute = now - datetime.timedelta(minutes=1)
        with freeze_time(last_minute):
            baker.make("issue_events.IssueEvent", issue__project=self.project)

        two_minutes_ago = now - datetime.timedelta(minutes=2)
        with freeze_time(two_minutes_ago):
            baker.make("issue_events.IssueEvent", issue__project=self.project)

        yesterday = now - datetime.timedelta(days=1)
        with freeze_time(yesterday):
            baker.make("issue_events.IssueEvent", issue__project=self.project)

        url = self.list_url
        with freeze_time(now):
            res = self.client.get(url, {"start": "now-1m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

        with freeze_time(now):
            res = self.client.get(url, {"start": "now-2m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        with freeze_time(now):
            res = self.client.get(url, {"start": "now-24h", "end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 3)

        with freeze_time(now):
            res = self.client.get(url, {"end": "now-3m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

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
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_name: "BananaOS 7"},
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

        event_no_tags = baker.make(
            "issue_events.IssueEvent", issue__project=self.project
        )

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

    def test_filter_environment(self):
        environment1_name = "prod"
        environment2_name = "staging"

        key_environment = baker.make("issue_events.TagKey", key="environment")
        environment1_value = baker.make(
            "issue_events.TagValue", value=environment1_name
        )
        environment2_value = baker.make(
            "issue_events.TagValue", value=environment2_name
        )
        environment3_value = baker.make("issue_events.TagValue", value="dev")
        issue1 = baker.make(
            "issue_events.Issue",
            project=self.project,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue1,
            tag_key=key_environment,
            tag_value=environment1_value,
        )
        issue2 = baker.make(
            "issue_events.Issue",
            project=self.project,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue2,
            tag_key=key_environment,
            tag_value=environment2_value,
        )
        issue3 = baker.make("issue_events.Issue", project=self.project)
        baker.make(
            "issue_events.IssueTag",
            issue=issue3,
            tag_key=key_environment,
            tag_value=environment3_value,
        )
        res = self.client.get(
            self.list_url
            + f"?environment={environment1_name}&environment={environment2_name}"
        )
        data = res.json()
        self.assertEqual(len(data), 2)
        self.assertNotIn(str(issue3.id), [data[0]["id"], data[1]["id"]])

    def test_filter_by_level(self):
        """
        A user should be able to filter by issue levels.
        """
        level_warning = LogLevel.WARNING
        level_fatal = LogLevel.FATAL

        issue1 = baker.make(
            "issue_events.Issue", project=self.project, level=level_warning
        )
        issue2 = baker.make(
            "issue_events.Issue", project=self.project, level=level_fatal
        )
        baker.make("issue_events.Issue", project=self.project)

        res = self.client.get(self.list_url + f"?query=level:{level_warning.label}")
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(issue1.id))

        res = self.client.get(self.list_url + f"?query=level:{level_fatal.label}")
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(issue2.id))

        res = self.client.get(self.list_url)
        self.assertEqual(len(res.json()), 3)

    def test_issue_update(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        data = {"status": "resolved"}
        res = self.client.put(
            get_issue_url(issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.RESOLVED)

    def test_issue_delete(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        not_my_issue = baker.make("issue_events.Issue")

        res = self.client.delete(get_issue_url(issue.id))
        self.assertEqual(res.status_code, 204)

        res = self.client.delete(get_issue_url(not_my_issue.id))
        self.assertEqual(res.status_code, 404)

    def test_organizations_issue_update(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)
        data = {"status": "resolved"}
        res = self.client.put(
            get_organization_issue_url(self.organization.slug, issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.RESOLVED)

    def test_bulk_update(self):
        """Bulk update only supports Issue status"""
        issues = baker.make("issue_events.Issue", project=self.project, _quantity=2)
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        status_to_set = EventStatus.RESOLVED
        data = {"status": status_to_set.label}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, status_to_set.label)
        issues = Issue.objects.all()
        self.assertEqual(issues[0].status, status_to_set)
        self.assertEqual(issues[1].status, status_to_set)

    def test_bulk_delete_via_ids(self):
        """Bulk delete Issues with ids"""
        issues = baker.make("issue_events.Issue", project=self.project, _quantity=2)
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        self.client.delete(url)
        issues = Issue.objects.count()
        self.assertEqual(issues, 0)

    def test_issue_merge(self):
        issues = baker.make("issue_events.Issue", project=self.project, _quantity=2)
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        data = {"merge": 1}
        res = self.client.put(
            url,
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Issue.objects.filter(is_deleted=False).count(), 1)

    def test_bulk_delete_via_search(self):
        """Bulk delete Issues via search string"""
        project2 = baker.make("projects.Project", organization=self.organization)
        project2.teams.add(self.team)
        issue1 = baker.make(Issue, project=self.project)
        issue2 = baker.make(Issue, project=project2)
        url = f"{self.list_url}?query=is:unresolved&project={self.project.id}"
        self.client.delete(url)
        self.assertFalse(Issue.objects.filter(id=issue1.id).exists())
        self.assertTrue(Issue.objects.filter(id=issue2.id).exists())

    def test_bulk_update_query(self):
        """Bulk update only supports Issue status"""
        project2 = baker.make("projects.Project", organization=self.organization)
        project2.teams.add(self.team)
        issue1 = baker.make(Issue, project=self.project)
        issue2 = baker.make(Issue, project=project2)
        url = f"{self.list_url}?query=is:unresolved&project={self.project.id}"
        status_to_set = EventStatus.RESOLVED
        data = {"status": status_to_set.label}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, status_to_set.label)
        issue1.refresh_from_db()
        issue2.refresh_from_db()
        self.assertEqual(issue1.status, status_to_set)
        self.assertEqual(issue2.status, EventStatus.UNRESOLVED)

    @freeze_time("2025-06-19T17:47:00Z")
    def test_issue_stats_endpoint(self):
        """
        Test retrieving 24-hour statistics for a set of issues.
        """
        now = timezone.now()

        # Issue with stats both inside and outside the 24h window
        issue_with_stats = baker.make(
            "issue_events.Issue", project=self.project, count=100
        )
        # This stat is recent and should be in the response
        recent_stat = baker.make(
            "issue_events.IssueAggregate",
            issue=issue_with_stats,
            date=now - datetime.timedelta(hours=2),
            count=5,
        )
        # This stat is old and should be filtered out
        baker.make(
            "issue_events.IssueAggregate",
            issue=issue_with_stats,
            date=now - datetime.timedelta(hours=25),
            count=20,
        )

        # Issue with no recent statistics
        issue_without_stats = baker.make(
            "issue_events.Issue", project=self.project, count=50
        )

        # Issue belonging to another organization that should not appear
        baker.make("issue_events.Issue")

        # Make the API request
        # Construct the URL and query parameters
        url = reverse(
            "api:issue_stats",  # Adjust the name based on your URL resolver
            kwargs={"organization_slug": self.organization.slug},
        )
        query_params = f"?groups={issue_with_stats.id}&groups={issue_without_stats.id}"

        res = self.client.get(url + query_params)

        # Assertions
        self.assertEqual(res.status_code, 200)
        response_data = res.json()
        self.assertEqual(len(response_data), 2)

        # Convert list to a dict keyed by ID for easier assertions
        results_by_id = {item["id"]: item for item in response_data}

        # --- Assertions for the issue WITH stats ---
        self.assertIn(str(issue_with_stats.id), results_by_id)
        stats_data = results_by_id[str(issue_with_stats.id)]

        self.assertEqual(stats_data["count"], str(issue_with_stats.count))
        self.assertEqual(len(stats_data["stats"]["24h"]), 1)

        # Check the content of the stat point
        stat_point = stats_data["stats"]["24h"][0]
        self.assertEqual(stat_point[0], int(recent_stat.date.timestamp()))
        self.assertEqual(stat_point[1], recent_stat.count)

        # --- Assertions for the issue WITHOUT stats ---
        self.assertIn(str(issue_without_stats.id), results_by_id)
        no_stats_data = results_by_id[str(issue_without_stats.id)]

        self.assertEqual(no_stats_data["count"], str(issue_without_stats.count))
        self.assertEqual(
            len(no_stats_data["stats"]["24h"]), 0
        )  # Should be an empty list

    @freeze_time("2025-06-19T17:47:00Z")
    def test_issue_stats_endpoint_14d(self):
        """
        Test retrieving 14-day statistics, ensuring data is grouped by day.
        """
        now = timezone.now()

        # Create an issue to test against
        issue = baker.make("issue_events.Issue", project=self.project, count=250)

        # Stat from 2 days ago (should be included)
        baker.make(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=2, hours=5),
            count=10,
        )

        # Two stats from 5 days ago (should be aggregated into one point)
        baker.make(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=5, hours=8),
            count=20,
        )
        baker.make(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=5, hours=12),
            count=15,
        )  # Total for this day should be 35

        # Stat from 15 days ago (should be excluded from the result)
        baker.make(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=15),
            count=100,
        )

        url = reverse(
            "api:issue_stats",
            kwargs={"organization_slug": self.organization.slug},
        )
        query_params = f"?groups={issue.id}&statsPeriod=14d"

        res = self.client.get(url + query_params)

        self.assertEqual(res.status_code, 200)
        response_data = res.json()
        self.assertEqual(len(response_data), 1)

        stats_data = response_data[0]
        self.assertEqual(stats_data["id"], str(issue.id))
        self.assertEqual(stats_data["count"], str(issue.count))

        # The key should be "14d" for the daily stats
        self.assertIn("14d", stats_data["stats"])
        daily_stats = stats_data["stats"]["14d"]

        # Expecting 2 data points: one for 2 days ago, one for 5 days ago
        self.assertEqual(len(daily_stats), 2)

        # Sort results by timestamp to ensure consistent order for assertions
        daily_stats.sort(key=lambda x: x[0])

        # --- Assertions for the data point from 5 days ago ---
        day_minus_5_stat = daily_stats[0]
        expected_day_5_ts = (now - datetime.timedelta(days=5)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.assertEqual(day_minus_5_stat[0], int(expected_day_5_ts.timestamp()))
        self.assertEqual(day_minus_5_stat[1], 35)  # 20 + 15

        # --- Assertions for the data point from 2 days ago ---
        day_minus_2_stat = daily_stats[1]
        expected_day_2_ts = (now - datetime.timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.assertEqual(day_minus_2_stat[0], int(expected_day_2_ts.timestamp()))
        self.assertEqual(day_minus_2_stat[1], 10)


class IssueEventAPIPermissionTestCase(APIPermissionTestCase):
    def setUp(self):
        self.create_org_team_project()
        self.set_client_credentials(self.auth_token.token)
        self.issue = baker.make("issue_events.Issue", project=self.project)

        self.list_url = reverse(
            "api:list_issues", kwargs={"organization_slug": self.organization.slug}
        )

    def test_list(self):
        self.assertGetReqStatusCode(self.list_url, 403)
        self.auth_token.add_permission("event:read")
        self.assertGetReqStatusCode(self.list_url, 200)


class IssueEventTagsAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def get_url(self, issue_id: int) -> str:
        return reverse("api:list_issue_tags", kwargs={"issue_id": issue_id})

    def test_issue_tags(self):
        issue = baker.make("issue_events.Issue", project=self.project)

        key_foo = baker.make("issue_events.TagKey", key="foo")
        key_animal = baker.make("issue_events.TagKey", key="animal")
        value_bar = baker.make("issue_events.TagValue", value="bar")
        value_cat = baker.make("issue_events.TagValue", value="cat")
        value_dog = baker.make("issue_events.TagValue", value="dog")

        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_bar,
            count=2,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_bar,
            count=1,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_cat,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_dog,
            count=4,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_cat,
            count=4,
        )

        url = self.get_url(issue.id)
        res = self.client.get(url)
        data = res.json()

        # Order is random
        if data[0]["name"] == "animal":
            animal = data[0]
            foo = data[1]
        else:
            animal = data[1]
            foo = data[0]

        self.assertEqual(animal["totalValues"], 5)
        self.assertEqual(animal["topValues"][0]["value"], "dog")
        self.assertEqual(animal["topValues"][0]["count"], 4)
        self.assertEqual(animal["uniqueValues"], 2)

        self.assertEqual(foo["totalValues"], 7)
        self.assertEqual(foo["topValues"][0]["value"], "cat")
        self.assertEqual(foo["topValues"][0]["count"], 4)
        self.assertEqual(foo["uniqueValues"], 2)

    def test_issue_tags_filter(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        value_bar = baker.make("issue_events.TagValue", value="bar")
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key__key="foo",
            tag_value=value_bar,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key__key="lol",
            tag_value=value_bar,
        )
        baker.make(
            "issue_events.IssueEvent", issue=issue, tags={"foo": "bar", "lol": "bar"}
        )
        url = self.get_url(issue.id)
        res = self.client.get(url + "?key=foo")
        self.assertEqual(len(res.json()), 1)

    def test_issue_tags_performance(self):
        issue = baker.make("issue_events.Issue", project=self.project)
        key_foo = baker.make("issue_events.TagKey", key="foo")
        key_animal = baker.make("issue_events.TagKey", key="animal")
        value_bar = baker.make("issue_events.TagValue", value="bar")
        value_cat = baker.make("issue_events.TagValue", value="cat")
        value_dog = baker.make("issue_events.TagValue", value="dog")
        quantity = 2

        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_bar,
            count=5,
            _quantity=quantity,
            _bulk_create=True,
        )
        baker.make(
            "issue_events.IssueTag",
            tag_key=key_foo,
            tag_value=value_bar,
            _quantity=quantity,
            _bulk_create=True,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_cat,
            count=5,
            _quantity=quantity,
            _bulk_create=True,
        )
        baker.make(
            "issue_events.IssueTag",
            _quantity=quantity,
            _bulk_create=True,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_dog,
            count=5,
            _quantity=quantity,
            _bulk_create=True,
        )

        url = self.get_url(issue.id)
        with self.assertNumQueries(2):  # Includes many auth related queries
            start = timer()
            self.client.get(url)
            end = timer()
        logger.info(end - start)
