from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class FilterTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("issue-list")

    def test_filter_by_date(self):
        """ A user should be able to filter by start and end datetimes. """
        with freeze_time(timezone.datetime(1999, 1, 1)):
            event1 = baker.make("events.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2010, 1, 1)):
            event2 = baker.make("events.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2020, 1, 1)):
            event3 = baker.make("events.Event", issue__project=self.project)
        res = self.client.get(
            self.url + "?start=2000-01-01T05:00:00.000Z&end=2019-01-01T05:00:00.000Z"
        )
        self.assertContains(res, event2.issue.title)
        self.assertNotContains(res, event1.issue.title)
        self.assertNotContains(res, event3.issue.title)

    def test_tag_space(self):
        tag_name = "os.name"
        tag_value = "Linux Vista"
        event = baker.make(
            "events.Event",
            issue__project=self.project,
            tags={tag_name: tag_value, "foo": "bar"},
        )
        event2 = baker.make(
            "events.Event", issue__project=self.project, tags={tag_name: "BananaOS 7"}
        )
        res = self.client.get(self.url + f'?query={tag_name}:"Linux+Vista" foo:bar')
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, event2.issue.title)

    def test_filter_by_tag(self):
        tag_browser = "browser.name"
        tag_value_firefox = "Firefox"
        tag_value_chrome = "Chrome"
        tag_value_cthulhu = "Cthulhu"
        tag_mythic_animal = "mythic_animal"

        event_only_firefox = baker.make(
            "events.Event",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        issue_only_firefox = event_only_firefox.issue
        event_only_firefox2 = baker.make(
            "events.Event",
            issue=issue_only_firefox,
            tags={tag_mythic_animal: tag_value_cthulhu},
        )

        event_firefox_chrome = baker.make(
            "events.Event",
            issue__project=self.project,
            tags={tag_browser: tag_value_firefox},
        )
        issue_firefox_chrome = event_firefox_chrome.issue
        event_firefox_chrome2 = baker.make(
            "events.Event",
            issue=issue_firefox_chrome,
            tags={tag_browser: tag_value_chrome},
        )

        event_no_tags = baker.make("events.Event", issue__project=self.project)

        event_browser_chrome_mythic_animal_firefox = baker.make(
            "events.Event",
            issue__project=self.project,
            tags={tag_mythic_animal: tag_value_firefox, tag_browser: tag_value_chrome},
        )

        res = self.client.get(self.url + f'?query={tag_browser}:"{tag_value_firefox}"')
        self.assertContains(res, event_only_firefox.issue.title)
        self.assertContains(res, issue_firefox_chrome.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

        # Browser is Firefox AND Chrome
        res = self.client.get(
            self.url
            + f"?query={tag_browser}:{tag_value_firefox} {tag_browser}:{tag_value_chrome}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertContains(res, issue_firefox_chrome.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

        # Browser mythic_animal is Firefox
        res = self.client.get(
            self.url + f"?query={tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, issue_firefox_chrome.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Chrome AND mythic_animal is Firefox
        res = self.client.get(
            self.url
            + f"?query={tag_browser}:{tag_value_chrome} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, issue_firefox_chrome.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertContains(res, event_browser_chrome_mythic_animal_firefox.issue.title)

        # Browser is Firefox AND mythic_animal is Firefox
        res = self.client.get(
            self.url
            + f"?query={tag_browser}:{tag_value_firefox} {tag_mythic_animal}:{tag_value_firefox}"
        )
        self.assertNotContains(res, event_only_firefox.issue.title)
        self.assertNotContains(res, issue_firefox_chrome.title)
        self.assertNotContains(res, event_no_tags.issue.title)
        self.assertNotContains(
            res, event_browser_chrome_mythic_animal_firefox.issue.title
        )

    def test_filter_by_tag_distinct(self):
        tag_browser = "browser.name"
        tag_value = "Firefox"
        tag_value2 = "Chrome"
        event = baker.make(
            "events.Event", issue__project=self.project, tags={tag_browser: tag_value}
        )
        baker.make(
            "events.Event",
            issue=event.issue,
            tags={tag_browser: tag_value},
            _quantity=2,
        )
        baker.make(
            "events.Event",
            issue=event.issue,
            tags={tag_browser: tag_value, tag_browser: tag_value2},
            _quantity=5,
        )
        baker.make(
            "events.Event",
            issue=event.issue,
            tags={tag_browser: tag_value2},
            _quantity=5,
        )

        res = self.client.get(self.url + f'?query={tag_browser}:"{tag_value}"')
        self.assertEqual(len(res.data), 1)


class SearchTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("issue-list")

    def test_search(self):
        event = baker.make(
            "events.Event", issue__project=self.project, data={"name": "apple sauce"}
        )
        event2 = baker.make(
            "events.Event", issue=event.issue, data={"name": "apple sauce"}
        )
        other_event = baker.make("events.Event", issue__project=self.project)
        res = self.client.get(self.url + "?query=is:unresolved apple+sauce")
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, other_event.issue.title)

        res = self.client.get(self.url + "?query=is:unresolved apple sauce")
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, other_event.issue.title)

        res = self.client.get(self.url + '?query=is:unresolved "apple sauce"')
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, other_event.issue.title)
