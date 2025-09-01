from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import IssueHash


class IssueAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()
        cls.issue_hash = baker.make(
            "issue_events.IssueHash", project=cls.project, issue__project=cls.project
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_issue_hashes(self):
        baker.make(
            "issue_events.IssueEvent",
            issue=self.issue_hash.issue,
            hashes=[self.issue_hash.value.hex],
        )
        list_url = reverse(
            "api:list_issue_hashes",
            kwargs={
                "organization_slug": self.organization.slug,
                "issue_id": self.issue_hash.issue_id,
            },
        )
        res = self.client.get(list_url)
        self.assertEqual(res.json()[0]["id"], self.issue_hash.value.hex)

    def test_delete_issue_hashes(self):
        list_url = reverse(
            "api:list_issue_hashes",
            kwargs={
                "organization_slug": self.organization.slug,
                "issue_id": self.issue_hash.issue_id,
            },
        )
        res = self.client.delete(
            list_url, query_params={"id": [self.issue_hash.value.hex]}
        )
        self.assertEqual(res.status_code, 202)
        self.assertFalse(IssueHash.objects.filter(id=self.issue_hash.id).exists())
