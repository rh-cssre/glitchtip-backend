from django.shortcuts import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import APIPermissionTestCase


class StatsAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.event = baker.make(
            "events.Event", issue__project__organization=self.organization
        )
        self.url = reverse(
            "stats-v2", kwargs={"organization_slug": self.organization.slug}
        )

    def test_get(self):
        self.assertGetReqStatusCode(self.url, 403)
        self.auth_token.add_permission("org:read")
        self.assertGetReqStatusCode(self.url, 400)
