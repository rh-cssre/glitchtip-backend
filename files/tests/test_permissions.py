from glitchtip.test_utils.test_case import APIPermissionTestCase
from django.shortcuts import reverse


class ChunkUploadAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.url = reverse(
            "chunk-upload", kwargs={"organization_slug": self.organization}
        )

    def test_get(self):
        self.assertGetReqStatusCode(self.url, 403)
        self.auth_token.add_permission("project:read")
        self.assertGetReqStatusCode(self.url, 200)

    def test_post(self):
        self.assertGetReqStatusCode(self.url, 403)
        self.auth_token.add_permission("project:write")
        self.assertGetReqStatusCode(self.url, 200)

