from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase


class IssueAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_issue_hashes(self):
        issue_hash = baker.make("issue_events.IssueHash", issue__project=self.project)
        list_url = reverse(
            "api:list_issue_hashes",
            kwargs={
                "organization_slug": self.organization.slug,
                "issue_id": issue_hash.issue_id,
            },
        )
        res = self.client.get(list_url)
        self.assertEqual(res.json(), [{"id": issue_hash.value.hex}])
