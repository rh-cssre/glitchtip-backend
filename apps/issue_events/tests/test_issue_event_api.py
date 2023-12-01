import re

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


class IssueEventAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()

    def test_multi_page_list(self):
        first_event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        baker.make("issue_events.IssueEvent", issue__project=self.project, issue_id=first_event.issue_id, _quantity=50)
        last_event = baker.make("issue_events.IssueEvent", issue__project=self.project, issue_id=first_event.issue_id)
        url = reverse("api:issue_event_list", args=[first_event.issue_id])

        with self.assertNumQueries(2):
            res = self.client.get(url)

        self.assertEqual(res.headers.get("X-Hits"), "52")
        self.assertEqual(res.json()[0]["id"], last_event.pk.hex)
        self.assertNotContains(res, first_event.pk.hex)

        pattern = r'(?<=\<).+?(?=\>)'
        links = re.findall(pattern, res.headers.get("Link"))

        res = self.client.get(links[1])

        self.assertEqual(res.headers.get("X-Hits"), "52")
        self.assertEqual(res.json()[-1]["id"], first_event.pk.hex)
        self.assertNotContains(res, last_event.pk.hex)

    def test_single_page_list(self):
        """
        Single page query should not hit DB for count
        """
        first_event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        last_event = baker.make("issue_events.IssueEvent", issue__project=self.project, issue_id=first_event.issue_id)
        url = reverse("api:issue_event_list", args=[first_event.issue_id])

        with self.assertNumQueries(1):
            res = self.client.get(url)

        self.assertEqual(res.headers.get("X-Hits"), "2")
        self.assertContains(res, last_event.pk.hex)
        self.assertContains(res, first_event.pk.hex)

    def test_retrieve(self):
        event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        url = reverse(
            "api:issue_event_retrieve",
            kwargs={"issue_id": event.issue_id, "event_id": "a" * 32},
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 404)

        url = reverse(
            "api:issue_event_retrieve",
            kwargs={"issue_id": event.issue_id, "event_id": event.id},
        )
        res = self.client.get(url)
        self.assertContains(res, event.pk.hex)

        url = reverse("api:issue_event_latest", kwargs={"issue_id": event.issue_id})
        res = self.client.get(url)
        self.assertContains(res, event.pk.hex)

    def test_authentication(self):
        url = reverse("api:issue_event_list", args=[1])
        self.client.logout()
        res = self.client.get(url)
        self.assertEqual(res.status_code, 401)
