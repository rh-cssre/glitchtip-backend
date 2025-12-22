from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase


class SourceCodeAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)

    def test_assemble(self):
        version = "app@v1"
        baker.make("releases.Release", version=version, organization=self.organization)
        url = reverse("api:artifact_bundle_assemble", args=[self.organization.slug])
        data = {
            "checksum": "94bc085fe32db9b4b1b82236214d65eeeeeeeeee",
            "chunks": ["94bc085fe32db9b4b1b82236214d65eeeeeeeeee"],
            "projects": [],
            "version": version,
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
