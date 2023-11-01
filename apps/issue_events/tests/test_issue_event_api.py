from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin


class IssueEventAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        super().create_logged_in_user()

    def test_list(self):
        event = baker.make("issue_events.IssueEvent", issue__project=self.project)
        baker.make("issue_events.IssueEvent", issue__project=self.project, _quantity=3)
        not_my_event = baker.make("issue_events.IssueEvent")
        url = reverse("api:issue_event_list", args=[event.issue_id])

        with self.assertNumQueries(1):
            res = self.client.get(url)
        self.assertContains(res, event.pk.hex)
        self.assertNotContains(res, not_my_event.pk.hex)

    def test_authentication(self):
        url = reverse("api:issue_event_list", args=[1])
        self.client.logout()
        res = self.client.get(url)
        self.assertEqual(res.status_code, 401)
