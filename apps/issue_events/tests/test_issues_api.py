import datetime
import logging
import re
import uuid
from timeit import default_timer as timer

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db.models import Value
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.issue import amake_issue, arefresh_issue
from glitchtip.test_utils.test_case import (
    APIPermissionTestCase,
    GlitchTestCase,
)

from ..constants import EventStatus, LogLevel
from ..models import Issue, IssueIndex

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
        self.async_client.force_login(self.user)

    async def test_retrieve(self):
        issue = await amake_issue(project=self.project, short_id=1)
        event = await baker.amake("issue_events.IssueEvent", issue=issue)
        await baker.amake(
            "issue_events.UserReport",
            project=self.project,
            issue=issue,
            event_id=event.id.hex,
            _quantity=1,
        )
        await baker.amake("issue_events.Comment", issue=issue, _quantity=3)
        url = reverse(
            "api:get_issue",
            kwargs={"issue_id": issue.id},
        )

        res = await self.async_client.get(url)
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

    async def test_retrieve_with_first_release(self):
        release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="1.0.0",
        )
        await release.projects.aadd(self.project)
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            short_id=1,
            first_release=release,
        )
        url = reverse("api:get_issue", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        data = res.json()
        self.assertIsNotNone(data.get("firstRelease"))
        self.assertEqual(data["firstRelease"]["version"], "1.0.0")
        self.assertEqual(data["firstRelease"]["shortVersion"], "1.0.0")
        self.assertIn("dateCreated", data["firstRelease"])

    async def test_retrieve_without_first_release(self):
        issue = await baker.amake("issue_events.Issue", project=self.project, short_id=1)
        url = reverse("api:get_issue", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        data = res.json()
        self.assertIsNone(data.get("firstRelease"))

    async def test_list(self):
        res = await self.async_client.get(self.list_url)
        self.assertEqual(res.status_code, 200)

        not_my_issue = await baker.amake("issue_events.Issue")
        issue = await baker.amake("issue_events.Issue", project=self.project, short_id=1)
        await baker.amake("issue_events.IssueEvent", issue=issue)
        res = await self.async_client.get(self.list_url)
        self.assertContains(res, issue.title)
        self.assertNotContains(res, not_my_issue.title)
        self.assertEqual(len(res.json()), 1)

    async def test_project_issue_list(self):
        not_my_project = await baker.amake(
            "projects.Project", organization=self.organization
        )
        not_my_issue = await baker.amake("issue_events.Issue", project=not_my_project)
        issue = await baker.amake("issue_events.Issue", project=self.project, short_id=1)
        await baker.amake("issue_events.IssueEvent", issue=issue)

        url = reverse(
            "api:list_project_issues",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_slug": self.project.slug,
            },
        )
        res = await self.async_client.get(url)
        self.assertContains(res, issue.title)
        self.assertNotContains(res, not_my_issue.title)
        self.assertEqual(len(res.json()), 1)

    async def test_filter_by_date(self):
        """
        A user should be able to filter by start and end datetimes.
        In the future, this should filter events, not first_seen.
        """
        issue1 = await baker.amake(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(1999, 1, 1)),
            project=self.project,
        )
        issue2 = await baker.amake(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(2010, 1, 1)),
            project=self.project,
        )
        issue3 = await baker.amake(
            "issue_events.Issue",
            first_seen=timezone.make_aware(timezone.datetime(2020, 1, 1)),
            project=self.project,
        )
        res = await self.async_client.get(
            self.list_url
            + "?start=2000-01-01T05:00:00.000Z&end=2019-01-01T05:00:00.000Z"
        )
        self.assertContains(res, issue2.title)
        self.assertNotContains(res, issue1.title)
        self.assertNotContains(res, issue3.title)

    async def test_sort(self):
        issue1 = await baker.amake("issue_events.Issue", project=self.project)
        issue2 = await baker.amake("issue_events.Issue", project=self.project)
        await IssueIndex.objects.filter(issue=issue2).aupdate(count=2)
        issue3 = await baker.amake("issue_events.Issue", project=self.project)

        res = await self.async_client.get(self.list_url)
        self.assertEqual(res.json()[0]["id"], str(issue3.id))

        res = await self.async_client.get(self.list_url + "?sort=-count")
        self.assertEqual(res.json()[0]["id"], str(issue2.id))

        res = await self.async_client.get(self.list_url + "?sort=priority")
        self.assertEqual(res.json()[0]["id"], str(issue1.id))

        res = await self.async_client.get(self.list_url + "?sort=-priority")
        self.assertEqual(res.json()[0]["id"], str(issue2.id))

    async def test_priority_environment(self):
        await baker.amake("issue_events.Issue", project=self.project)
        res = await self.async_client.get(
            self.list_url + "?sort=-priority&environment=env"
        )
        self.assertEqual(res.status_code, 200)

    async def test_paginated_list_sorted_by_index_field(self):
        """The list sorts by count/last_seen, which live on the IssueIndex leaf
        (``index__count`` / ``index__last_seen``). The cursor paginator builds
        the next-page position from the last row on the page, so a result set
        spanning more than one page must resolve the ordering value through the
        relation rather than 500ing on a flat getattr.
        """
        # Distinct count/last_seen so the ordering (and the cursor position
        # filter) is unambiguous: issues[0] is oldest/smallest, issues[2] newest.
        base = timezone.make_aware(timezone.datetime(2020, 1, 1))
        issues = [
            await baker.amake("issue_events.Issue", project=self.project) for _ in range(3)
        ]
        for i, issue in enumerate(issues):
            await IssueIndex.objects.filter(issue=issue).aupdate(
                count=i + 1, last_seen=base + datetime.timedelta(hours=i)
            )

        # limit=2 forces a second page from 3 issues, exercising the next-page
        # cursor position extraction off the joined index field (the regression:
        # this 500'd on a flat getattr of "index__last_seen"/"index__count").
        # Default sort is -last_seen, so the newest two land on the first page.
        for sort, expected_first_page in (
            ("", [issues[2].id, issues[1].id]),
            ("&sort=last_seen", [issues[0].id, issues[1].id]),
            ("&sort=-count", [issues[2].id, issues[1].id]),
        ):
            res = await self.async_client.get(self.list_url + f"?limit=2{sort}")
            self.assertEqual(res.status_code, 200, msg=f"sort={sort!r}: {res.content}")
            page1 = [item["id"] for item in res.json()]
            self.assertEqual(page1, [str(i) for i in expected_first_page])
            self.assertIn('rel="next"; results="true"', res["Link"])

            # Follow the next link and assert the two pages together cover every
            # issue exactly once (no row dropped or duplicated across the cursor).
            next_url = re.search(r'<([^>]+)>; rel="next"', res["Link"]).group(1)
            res2 = await self.async_client.get(next_url)
            self.assertEqual(res2.status_code, 200)
            page2 = [item["id"] for item in res2.json()]
            self.assertEqual(sorted(page1 + page2), sorted(str(i.id) for i in issues))

    async def _set_search_document(self, issue, text):
        """Populate an issue's IssueIndex row (the full-text store).

        Two steps: the SearchVector expression is applied via update() (it does
        not resolve through Model.save()).
        """
        await IssueIndex.objects.aget_or_create(
            issue=issue, organization_id=self.organization.id
        )
        await IssueIndex.objects.filter(issue=issue).aupdate(
            fts_document=SearchVector(Value(text))
        )

    async def test_search(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        await self._set_search_document(issue, "apple sauce")
        event = await baker.amake("issue_events.IssueEvent", issue=issue)
        other_issue = await baker.amake("issue_events.Issue", project=self.project)

        res = await self.async_client.get(
            self.list_url + "?query=is:unresolved apple+sauce"
        )
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        # Not sure how to do this in Ninja without always removing None field values
        # self.assertNotContains(res, "matchingEventId")
        self.assertNotIn("X-Sentry-Direct-Hit", res.headers)

        res = await self.async_client.get(
            self.list_url + "?query=is:unresolved apple sauce"
        )
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)

        res = await self.async_client.get(
            self.list_url + '?query=is:unresolved "apple sauce"'
        )
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)

        res = await self.async_client.get(self.list_url + "?query=" + event.id.hex)
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        self.assertContains(res, "matchingEventId")
        self.assertContains(res, event.id.hex)
        self.assertEqual(res.headers.get("X-Sentry-Direct-Hit"), "1")

        # Search by client-provided sentry SDK event_id (UUIDv4)
        sentry_event_id = uuid.uuid4()
        await baker.amake(
            "issue_events.IssueEvent",
            issue=issue,
            event_id=sentry_event_id,
            organization=self.organization,
        )
        res = await self.async_client.get(
            self.list_url + "?query=" + sentry_event_id.hex
        )
        self.assertContains(res, issue.title)
        self.assertNotContains(res, other_issue.title)
        self.assertContains(res, "matchingEventId")
        self.assertEqual(res.headers.get("X-Sentry-Direct-Hit"), "1")

        event3 = await baker.amake(
            "issue_events.IssueEvent", issue=issue, data={"name": "plum sauce"}
        )
        # A later event extends the issue's search document (same as ingest's
        # append path appending to fts_document).
        await self._set_search_document(issue, "apple sauce plum sauce")
        res = await self.async_client.get(
            self.list_url + '?query=is:unresolved "plum sauce"'
        )
        self.assertContains(res, event3.issue.title)
        res = await self.async_client.get(
            self.list_url + '?query=is:unresolved "apple sauce"'
        )
        self.assertContains(res, event.issue.title)

    async def test_search_via_decoupled_index(self):
        """
        Search resolves through IssueIndex.fts_document, the sole
        full-text store now that Issue.search_vector is dropped. Guards
        against the index being populated with a corrupted (re-tokenized)
        tsvector and against the org-scoped partition-pruning join.
        """
        issue = await baker.amake("issue_events.Issue", project=self.project)
        # The post_save signal already created the leaf row; just set the vector.
        await IssueIndex.objects.filter(issue=issue).aupdate(
            fts_document=SearchVector(Value("kangaroo marsupial"))
        )
        other_issue = await baker.amake("issue_events.Issue", project=self.project)

        async def ids(query):
            res = await self.async_client.get(self.list_url + "?query=" + query)
            self.assertEqual(res.status_code, 200)
            return {int(row["id"]) for row in res.json()}

        self.assertEqual(await ids("is:unresolved kangaroo"), {issue.id})
        self.assertNotIn(other_issue.id, await ids("is:unresolved kangaroo"))
        self.assertEqual(await ids('is:unresolved "kangaroo marsupial"'), {issue.id})

    async def test_search_unmatched_quote(self):
        """Queries with unmatched quotes should not raise ValueError"""
        await baker.amake("issue_events.Issue", project=self.project)
        res = await self.async_client.get(
            self.list_url + "?query=SMTPAuthenticationError: (534, b'5.7.8"
        )
        self.assertEqual(res.status_code, 200)

    async def test_search_wildcard(self):
        issue_str = "The foo want to the bar"
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            title=issue_str,
        )
        await self._set_search_document(issue, issue_str)
        res = await self.async_client.get(self.list_url + "?query=is:unresolved f*o")
        self.assertContains(res, issue.title)
        res = await self.async_client.get(self.list_url + "?query=is:unresolved f*x")
        self.assertNotContains(res, issue.title)

    async def test_list_relative_datetime_filter(self):
        now = timezone.now()
        last_minute = now - datetime.timedelta(minutes=1)
        with freeze_time(last_minute):
            await baker.amake("issue_events.IssueEvent", issue__project=self.project)

        two_minutes_ago = now - datetime.timedelta(minutes=2)
        with freeze_time(two_minutes_ago):
            await baker.amake("issue_events.IssueEvent", issue__project=self.project)

        yesterday = now - datetime.timedelta(days=1)
        with freeze_time(yesterday):
            await baker.amake("issue_events.IssueEvent", issue__project=self.project)

        url = self.list_url
        with freeze_time(now):
            res = await self.async_client.get(url, {"start": "now-1m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

        with freeze_time(now):
            res = await self.async_client.get(url, {"start": "now-2m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        with freeze_time(now):
            res = await self.async_client.get(url, {"start": "now-24h", "end": "now"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 3)

        with freeze_time(now):
            res = await self.async_client.get(url, {"end": "now-3m"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

    async def test_tag_space(self):
        tag_name = "os.name"
        tag_value = "Linux Vista"
        event = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_name: tag_value, "foo": "bar"},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key__key=tag_name,
            tag_value__value=tag_value,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key__key="foo",
            tag_value__value="bar",
        )
        event2 = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_name: "BananaOS 7"},
        )

        res = await self.async_client.get(
            self.list_url + f'?query={tag_name}:"Linux+Vista" foo:bar'
        )
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, event2.issue.title)

    async def test_filter_by_tag(self):
        tag_browser = "browser.name"
        tag_value_firefox = "Firefox"
        tag_value_chrome = "Chrome"
        tag_value_cthulhu = "Cthulhu"
        tag_mythic_animal = "mythic_animal"

        key_browser = await baker.amake("issue_events.TagKey", key=tag_browser)
        key_mythic_animal = await baker.amake(
            "issue_events.TagKey", key=tag_mythic_animal
        )
        value_firefox = await baker.amake(
            "issue_events.TagValue", value=tag_value_firefox
        )
        value_chrome = await baker.amake(
            "issue_events.TagValue", value=tag_value_chrome
        )
        value_cthulhu = await baker.amake(
            "issue_events.TagValue", value=tag_value_cthulhu
        )

        event_only_firefox = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_only_firefox.issue,
            tag_key=key_browser,
            tag_value=value_firefox,
        )

        event_only_firefox2 = await baker.amake(
            "issue_events.IssueEvent",
            issue=event_only_firefox.issue,
            tags={tag_mythic_animal: tag_value_cthulhu},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_only_firefox2.issue,
            tag_key=key_mythic_animal,
            tag_value=value_cthulhu,
        )

        event_firefox_chrome = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_firefox_chrome.issue,
            tag_key=key_browser,
            tag_value=value_firefox,
        )

        event_firefox_chrome2 = await baker.amake(
            "issue_events.IssueEvent",
            issue=event_firefox_chrome.issue,
            tags={tag_browser: tag_value_chrome},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_firefox_chrome2.issue,
            tag_key=key_browser,
            tag_value=value_chrome,
        )

        event_no_tags = await baker.amake(
            "issue_events.IssueEvent", issue__project=self.project
        )

        event_browser_chrome_mythic_animal_firefox = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_mythic_animal: tag_value_firefox, tag_browser: tag_value_chrome},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_browser_chrome_mythic_animal_firefox.issue,
            tag_key=key_mythic_animal,
            tag_value=value_firefox,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event_browser_chrome_mythic_animal_firefox.issue,
            tag_key=key_browser,
            tag_value=value_chrome,
        )

        url = self.list_url
        res = await self.async_client.get(
            url + f'?query={tag_browser}:"{tag_value_firefox}"'
        )
        self.assertContains(res, event_only_firefox.issue.title)
        self.assertContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

        # Browser is Firefox AND Chrome
        res = await self.async_client.get(
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
        res = await self.async_client.get(
            url + f"?query={tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Chrome AND mythic_animal is Firefox
        res = await self.async_client.get(
            url
            + f"?query={tag_browser}:{tag_value_chrome} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Firefox AND mythic_animal is Firefox
        res = await self.async_client.get(
            url
            + f"?query={tag_browser}:{tag_value_firefox} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, event_firefox_chrome.issue.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

    async def test_filter_by_tag_distinct(self):
        tag_browser = "browser.name"
        tag_value = "Firefox"
        tag_value2 = "Chrome"

        key_browser = await baker.amake("issue_events.TagKey", key=tag_browser)
        value = await baker.amake("issue_events.TagValue", value=tag_value)
        value2 = await baker.amake("issue_events.TagValue", value=tag_value2)

        event = await baker.amake(
            "issue_events.IssueEvent",
            issue__project=self.project,
            tags={tag_browser: tag_value},
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        await baker.amake(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value},
            _quantity=2,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        await baker.amake(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value},
            _quantity=5,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value2,
        )
        await baker.amake(
            "issue_events.IssueEvent",
            issue=event.issue,
            tags={tag_browser: tag_value2},
            _quantity=5,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=event.issue,
            tag_key=key_browser,
            tag_value=value2,
        )

        res = await self.async_client.get(
            self.list_url + f'?query={tag_browser}:"{tag_value}"'
        )
        self.assertEqual(len(res.json()), 1)

    async def test_filter_environment(self):
        environment1_name = "prod"
        environment2_name = "staging"

        key_environment = await baker.amake("issue_events.TagKey", key="environment")
        environment1_value = await baker.amake(
            "issue_events.TagValue", value=environment1_name
        )
        environment2_value = await baker.amake(
            "issue_events.TagValue", value=environment2_name
        )
        environment3_value = await baker.amake("issue_events.TagValue", value="dev")
        issue1 = await baker.amake(
            "issue_events.Issue",
            project=self.project,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue1,
            tag_key=key_environment,
            tag_value=environment1_value,
        )
        issue2 = await baker.amake(
            "issue_events.Issue",
            project=self.project,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue2,
            tag_key=key_environment,
            tag_value=environment2_value,
        )
        issue3 = await baker.amake("issue_events.Issue", project=self.project)
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue3,
            tag_key=key_environment,
            tag_value=environment3_value,
        )
        res = await self.async_client.get(
            self.list_url
            + f"?environment={environment1_name}&environment={environment2_name}"
        )
        data = res.json()
        self.assertEqual(len(data), 2)
        self.assertNotIn(str(issue3.id), [data[0]["id"], data[1]["id"]])

    async def test_filter_by_level(self):
        """
        A user should be able to filter by issue levels.
        """
        level_warning = LogLevel.WARNING
        level_fatal = LogLevel.FATAL

        issue1 = await baker.amake("issue_events.Issue", project=self.project)
        await IssueIndex.objects.filter(issue=issue1).aupdate(level=level_warning)
        issue2 = await baker.amake("issue_events.Issue", project=self.project)
        await IssueIndex.objects.filter(issue=issue2).aupdate(level=level_fatal)
        await baker.amake("issue_events.Issue", project=self.project)

        res = await self.async_client.get(
            self.list_url + f"?query=level:{level_warning.label}"
        )
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(issue1.id))

        res = await self.async_client.get(
            self.list_url + f"?query=level:{level_fatal.label}"
        )
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(issue2.id))

        res = await self.async_client.get(self.list_url)
        self.assertEqual(len(res.json()), 3)

    async def test_issue_update(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"status": "resolved"}
        res = await self.async_client.put(
            get_issue_url(issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.RESOLVED)

    async def test_resolve_with_status_details_in_release(self):
        """PUT with statusDetails.inRelease sets resolved_in_release"""
        release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="1.0.0",
        )
        await release.projects.aadd(self.project)
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"status": "resolved", "statusDetails": {"inRelease": "1.0.0"}}
        res = await self.async_client.put(
            get_issue_url(issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.RESOLVED)
        self.assertEqual(issue.resolved_in_release_id, release.id)

    async def test_resolve_with_status_details_in_next_release(self):
        """PUT with statusDetails.inNextRelease sets resolved_in_release to latest release"""
        older_release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="0.9.0",
        )
        await older_release.projects.aadd(self.project)
        latest_release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="1.0.0",
        )
        await latest_release.projects.aadd(self.project)
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"status": "resolved", "statusDetails": {"inNextRelease": True}}
        res = await self.async_client.put(
            get_issue_url(issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.RESOLVED)
        self.assertEqual(issue.resolved_in_release_id, latest_release.id)

    async def test_status_details_in_response(self):
        """Resolved issue with resolved_in_release shows statusDetails.inRelease in GET"""
        release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="1.0.0",
        )
        await release.projects.aadd(self.project)
        issue = await amake_issue(
            project=self.project,
            short_id=1,
            status=EventStatus.RESOLVED,
            resolved_in_release=release,
        )
        url = reverse("api:get_issue", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        data = res.json()
        self.assertEqual(data["statusDetails"], {"inRelease": "1.0.0"})

    async def test_last_release_in_response(self):
        """Issue with last_release shows lastRelease object in GET"""
        release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="2.0.0",
        )
        await release.projects.aadd(self.project)
        issue = await amake_issue(
            project=self.project,
            short_id=1,
            last_release=release,
        )
        url = reverse("api:get_issue", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        data = res.json()
        self.assertIsNotNone(data.get("lastRelease"))
        self.assertEqual(data["lastRelease"]["version"], "2.0.0")
        self.assertEqual(data["lastRelease"]["shortVersion"], "2.0.0")

    async def test_unresolve_clears_resolved_in_release(self):
        """Un-resolving an issue should clear resolved_in_release"""
        release = await baker.amake(
            "releases.Release",
            organization=self.project.organization,
            version="1.0.0",
        )
        issue = await amake_issue(
            project=self.project,
            status=EventStatus.RESOLVED,
            resolved_in_release=release,
        )
        data = {"status": "unresolved"}
        res = await self.async_client.put(
            get_issue_url(issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)
        self.assertIsNone(issue.resolved_in_release_id)

    async def test_issue_delete(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        not_my_issue = await baker.amake("issue_events.Issue")

        res = await self.async_client.delete(get_issue_url(issue.id))
        self.assertEqual(res.status_code, 204)

        res = await self.async_client.delete(get_issue_url(not_my_issue.id))
        self.assertEqual(res.status_code, 404)

    async def test_organizations_issue_update(self):
        issue = await amake_issue(project=self.project)
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)
        data = {"status": "resolved"}
        res = await self.async_client.put(
            get_organization_issue_url(self.organization.slug, issue.pk),
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.RESOLVED)

    async def test_bulk_update(self):
        """Bulk update only supports Issue status"""
        issues = await baker.amake(
            "issue_events.Issue", project=self.project, _quantity=2
        )
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        status_to_set = EventStatus.RESOLVED
        data = {"status": status_to_set.label}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, status_to_set.label)
        async for issue in Issue.objects.all():
            index = await IssueIndex.objects.aget(issue_id=issue.pk)
            self.assertEqual(index.status, status_to_set)

    async def test_bulk_delete_via_ids(self):
        """Bulk delete Issues with ids"""
        issues = await baker.amake(
            "issue_events.Issue", project=self.project, _quantity=2
        )
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        await self.async_client.delete(url)
        issues = await Issue.objects.acount()
        self.assertEqual(issues, 0)

    async def test_issue_merge(self):
        issue_event_count = 2
        issues = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            _quantity=2,
        )
        # count lives on the IssueIndex leaf; set it to the number of
        # events we create per issue.
        await IssueIndex.objects.filter(issue__in=issues).aupdate(
            count=issue_event_count
        )
        await baker.amake(
            "issue_events.IssueEvent",
            issue=issues[0],
            _quantity=issue_event_count,
        )
        await baker.amake(
            "issue_events.IssueEvent",
            issue=issues[1],
            _quantity=issue_event_count,
        )
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        data = {"merge": 1}
        res = await self.async_client.put(
            url,
            data,
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(await Issue.objects.filter(is_deleted=False).acount(), 1)
        merged = await Issue.objects.aget(is_deleted=False)
        merged_index = await IssueIndex.objects.aget(issue_id=merged.pk)
        self.assertEqual(merged_index.count, 2 * issue_event_count)

    async def test_bulk_delete_via_search(self):
        """Bulk delete Issues via search string"""
        project2 = await baker.amake("projects.Project", organization=self.organization)
        await project2.teams.aadd(self.team)
        issue1 = await baker.amake(Issue, project=self.project)
        issue2 = await baker.amake(Issue, project=project2)
        url = f"{self.list_url}?query=is:unresolved&project={self.project.id}"
        await self.async_client.delete(url)
        self.assertFalse(await Issue.objects.filter(id=issue1.id).aexists())
        self.assertTrue(await Issue.objects.filter(id=issue2.id).aexists())

    async def test_bulk_update_query(self):
        """Bulk update only supports Issue status"""
        project2 = await baker.amake("projects.Project", organization=self.organization)
        await project2.teams.aadd(self.team)
        issue1 = await baker.amake(Issue, project=self.project)
        issue2 = await baker.amake(Issue, project=project2)
        url = f"{self.list_url}?query=is:unresolved&project={self.project.id}"
        status_to_set = EventStatus.RESOLVED
        data = {"status": status_to_set.label}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, status_to_set.label)
        await arefresh_issue(issue1)
        await arefresh_issue(issue2)
        self.assertEqual(issue1.status, status_to_set)
        self.assertEqual(issue2.status, EventStatus.UNRESOLVED)

    async def test_assign_to_user_by_id(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": f"user:{self.user.id}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await issue.arefresh_from_db()
        self.assertEqual(issue.assigned_to_org_user_id, self.org_user.id)
        self.assertIsNone(issue.assigned_to_team_id)
        body = res.json()
        self.assertEqual(body["assignedTo"]["type"], "user")
        self.assertEqual(body["assignedTo"]["id"], str(self.user.id))
        self.assertEqual(body["assignedTo"]["email"], self.user.email)

    async def test_assign_to_user_by_email(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": self.user.email}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await issue.arefresh_from_db()
        self.assertEqual(issue.assigned_to_org_user_id, self.org_user.id)

    async def test_assign_to_team(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": f"team:{self.team.slug}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await issue.arefresh_from_db()
        self.assertIsNone(issue.assigned_to_org_user_id)
        self.assertEqual(issue.assigned_to_team_id, self.team.id)
        body = res.json()
        self.assertEqual(body["assignedTo"]["type"], "team")
        self.assertEqual(body["assignedTo"]["slug"], self.team.slug)

    async def test_unassign(self):
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            assigned_to_org_user=self.org_user,
        )
        data = {"assignedTo": None}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await issue.arefresh_from_db()
        self.assertIsNone(issue.assigned_to_org_user_id)
        self.assertIsNone(issue.assigned_to_team_id)
        self.assertIsNone(res.json()["assignedTo"])

    async def test_assign_switches_team_to_user(self):
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            assigned_to_team=self.team,
        )
        data = {"assignedTo": f"user:{self.user.id}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await issue.arefresh_from_db()
        self.assertEqual(issue.assigned_to_org_user_id, self.org_user.id)
        self.assertIsNone(issue.assigned_to_team_id)

    async def test_assign_unassigns_when_membership_removed(self):
        """Removing an OrganizationUser SET_NULLs their issue assignments."""
        other_user = await baker.amake("users.user")
        other_org_user = await sync_to_async(self.organization.add_user)(other_user)
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            assigned_to_org_user=other_org_user,
        )
        await other_org_user.adelete()
        await issue.arefresh_from_db()
        self.assertIsNone(issue.assigned_to_org_user_id)

    async def test_assign_user_not_in_org_is_not_found(self):
        other_user = await baker.amake("users.user")
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": f"user:{other_user.id}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)
        await issue.arefresh_from_db()
        self.assertIsNone(issue.assigned_to_org_user_id)

    async def test_assign_team_from_other_org_is_not_found(self):
        other_team = await baker.amake("teams.Team")
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": f"team:{other_team.slug}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)

    async def test_assign_unknown_user_is_not_found(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": "nobody@nowhere.invalid"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)

    async def test_assign_pending_invite_is_not_found(self):
        """Pending invites (OrganizationUser.user is None) are not assignable."""
        pending = await baker.amake(
            "organizations_ext.OrganizationUser",
            organization=self.organization,
            user=None,
            email="pending@example.com",
        )
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": pending.email}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)

    async def test_assign_bad_user_id_format(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        data = {"assignedTo": "user:notanumber"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)

    async def test_assign_without_status_keeps_status(self):
        issue = await amake_issue(
            project=self.project,
            status=EventStatus.RESOLVED,
        )
        data = {"assignedTo": f"user:{self.user.id}"}
        res = await self.async_client.put(
            get_issue_url(issue.pk), data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        await arefresh_issue(issue)
        self.assertEqual(issue.status, EventStatus.RESOLVED)
        self.assertEqual(issue.assigned_to_org_user_id, self.org_user.id)

    async def test_bulk_assign(self):
        issues = await baker.amake(
            "issue_events.Issue", project=self.project, _quantity=2
        )
        url = f"{self.list_url}?id={issues[0].id}&id={issues[1].id}"
        data = {"assignedTo": f"user:{self.user.id}"}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        async for issue in Issue.objects.filter(id__in=[i.id for i in issues]):
            self.assertEqual(issue.assigned_to_org_user_id, self.org_user.id)

    # Kept synchronous: uses transaction.atomic() to contain the IntegrityError,
    # which is a sync-only context manager.
    def test_db_constraint_rejects_both_user_and_team(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                baker.make(
                    "issue_events.Issue",
                    project=self.project,
                    assigned_to_org_user=self.org_user,
                    assigned_to_team=self.team,
                )

    @freeze_time("2025-06-19T17:47:00Z")
    async def test_issue_stats_endpoint(self):
        """
        Test retrieving 24-hour statistics for a set of issues.
        """
        now = timezone.now()

        # Issue with stats both inside and outside the 24h window
        issue_with_stats = await amake_issue(project=self.project, count=100)
        # This stat is recent and should be in the response
        recent_stat = await baker.amake(
            "issue_events.IssueAggregate",
            issue=issue_with_stats,
            date=now - datetime.timedelta(hours=2),
            count=5,
        )
        # This stat is old and should be filtered out
        await baker.amake(
            "issue_events.IssueAggregate",
            issue=issue_with_stats,
            date=now - datetime.timedelta(hours=25),
            count=20,
        )

        # Issue with no recent statistics
        issue_without_stats = await amake_issue(project=self.project, count=50)

        # Issue belonging to another organization that should not appear
        await baker.amake("issue_events.Issue")

        # Make the API request
        # Construct the URL and query parameters
        url = reverse(
            "api:issue_stats",  # Adjust the name based on your URL resolver
            kwargs={"organization_slug": self.organization.slug},
        )
        query_params = f"?groups={issue_with_stats.id}&groups={issue_without_stats.id}"

        res = await self.async_client.get(url + query_params)

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
    async def test_issue_stats_endpoint_14d(self):
        """
        Test retrieving 14-day statistics, ensuring data is grouped by day.
        """
        now = timezone.now()

        # Create an issue to test against
        issue = await amake_issue(project=self.project, count=250)

        # Stat from 2 days ago (should be included)
        await baker.amake(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=2, hours=5),
            count=10,
        )

        # Two stats from 5 days ago (should be aggregated into one point)
        await baker.amake(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=5, hours=8),
            count=20,
        )
        await baker.amake(
            "issue_events.IssueAggregate",
            issue=issue,
            date=now - datetime.timedelta(days=5, hours=12),
            count=15,
        )  # Total for this day should be 35

        # Stat from 15 days ago (should be excluded from the result)
        await baker.amake(
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

        res = await self.async_client.get(url + query_params)

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


class IssueCommitsAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_list_issue_commits_no_release(self):
        issue = await baker.amake("issue_events.Issue", project=self.project, short_id=1)
        url = reverse("api:list_issue_commits", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    async def test_list_issue_commits_release_no_commits(self):
        release = await baker.amake(
            "releases.Release", organization=self.organization, data={}
        )
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            short_id=1,
            first_release=release,
        )
        url = reverse("api:list_issue_commits", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    async def test_list_issue_commits(self):
        commits = [
            {
                "id": "abc123",
                "message": "fix: login bug",
                "authorName": "Alice",
                "authorEmail": "alice@example.com",
            },
            {
                "id": "def456",
                "message": "feat: add logout",
                "authorName": "",
                "authorEmail": "",
            },
        ]
        release = await baker.amake(
            "releases.Release",
            organization=self.organization,
            data={"commits": commits},
        )
        issue = await baker.amake(
            "issue_events.Issue",
            project=self.project,
            short_id=1,
            first_release=release,
        )
        url = reverse("api:list_issue_commits", kwargs={"issue_id": issue.id})
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["id"], "abc123")
        self.assertEqual(data[0]["message"], "fix: login bug")
        self.assertEqual(data[0]["authorName"], "Alice")
        self.assertEqual(data[1]["id"], "def456")

    async def test_list_issue_commits_not_found(self):
        url = reverse("api:list_issue_commits", kwargs={"issue_id": 99999})
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 404)


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
        self.async_client.force_login(self.user)

    def get_url(self, issue_id: int) -> str:
        return reverse("api:list_issue_tags", kwargs={"issue_id": issue_id})

    async def test_issue_tags(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)

        key_foo = await baker.amake("issue_events.TagKey", key="foo")
        key_animal = await baker.amake("issue_events.TagKey", key="animal")
        value_bar = await baker.amake("issue_events.TagValue", value="bar")
        value_cat = await baker.amake("issue_events.TagValue", value="cat")
        value_dog = await baker.amake("issue_events.TagValue", value="dog")

        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_bar,
            count=2,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_bar,
            count=1,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_cat,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_animal,
            tag_value=value_dog,
            count=4,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_foo,
            tag_value=value_cat,
            count=4,
        )

        url = self.get_url(issue.id)
        res = await self.async_client.get(url)
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

    async def test_issue_tags_filter(self):
        issue = await baker.amake("issue_events.Issue", project=self.project)
        value_bar = await baker.amake("issue_events.TagValue", value="bar")
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key__key="foo",
            tag_value=value_bar,
        )
        await baker.amake(
            "issue_events.IssueTag",
            issue=issue,
            tag_key__key="lol",
            tag_value=value_bar,
        )
        await baker.amake(
            "issue_events.IssueEvent", issue=issue, tags={"foo": "bar", "lol": "bar"}
        )
        url = self.get_url(issue.id)
        res = await self.async_client.get(url + "?key=foo")
        self.assertEqual(len(res.json()), 1)

    # Kept synchronous: assertNumQueries cannot observe queries issued on the
    # async DB connection used by the async test client.
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
