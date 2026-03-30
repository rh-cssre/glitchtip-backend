from django.urls import reverse

from glitchtip.test_utils.test_case import APIPermissionTestCase

from .test_api import generate_file


class ChunkUploadAPIPermissionTests(APIPermissionTestCase):
    def setUp(self):
        self.create_user_org()
        self.set_client_credentials(self.auth_token.token)
        self.url = reverse("api:chunk_upload", args=[self.organization])

    def test_post(self):
        data = {"file_gzip": generate_file()}
        res = self.client.post(self.url, data, **self.get_headers())
        self.assertEqual(res.status_code, 403)
        self.auth_token.add_permission("project:write")
        res = self.client.post(self.url, data, **self.get_headers())
        self.assertEqual(res.status_code, 200)
