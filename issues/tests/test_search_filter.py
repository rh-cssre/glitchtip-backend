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
            event1 = baker.make("issues.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2010, 1, 1)):
            event2 = baker.make("issues.Event", issue__project=self.project)
        with freeze_time(timezone.datetime(2020, 1, 1)):
            event3 = baker.make("issues.Event", issue__project=self.project)
        res = self.client.get(
            self.url + "?start=2000-01-01T05:00:00.000Z&end=2019-01-01T05:00:00.000Z"
        )
        self.assertContains(res, event2.issue.title)
        self.assertNotContains(res, event1.issue.title)
        self.assertNotContains(res, event3.issue.title)


class SearchTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("issue-list")

    def test_search(self):
        event = baker.make(
            "issues.Event", issue__project=self.project, data={"name": "apple sauce"}
        )
        event2 = baker.make(
            "issues.Event", issue=event.issue, data={"name": "apple sauce"}
        )
        other_event = baker.make("issues.Event", issue__project=self.project)
        res = self.client.get(self.url + "?query=is:unresolved apple sauce")
        self.assertContains(res, event.issue.title)
        self.assertNotContains(res, other_event.issue.title)
