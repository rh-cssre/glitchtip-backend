from django.shortcuts import reverse
from model_bakery import baker
from glitchtip.test_utils.test_case import GlitchTipTestCase


class TransactionAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_list(self):
        url = reverse(
            "organization-transactions-list",
            kwargs={"organization_slug": self.organization.slug},
        )
        transaction = baker.make(
            "performance.TransactionEvent", group__project=self.project
        )
        res = self.client.get(url)
        self.assertContains(res, transaction.event_id)
