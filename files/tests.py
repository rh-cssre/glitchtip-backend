from io import BytesIO
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.shortcuts import reverse
from glitchtip.test_utils.test_case import GlitchTipTestCase
from glitchtip.test_utils.test_case import APIPermissionTestCase


class ChunkUploadAPITestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse(
            "chunk-upload", kwargs={"organization_slug": self.organization}
        )

    def test_get(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)

    def test_post(self):
        im_io = BytesIO()
        file = InMemoryUploadedFile(
            im_io, None, "random-name.jpg", "image/jpeg", len(im_io.getvalue()), None
        )
        data = {"file_gzip": file}
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 200)


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
