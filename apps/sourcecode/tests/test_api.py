from django.urls import reverse
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase


class SourceCodeAPITestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse(
            "api:artifact_bundle_assemble", args=[self.organization.slug]
        )

    def test_assemble_missing_chunks(self):
        """POST with chunks that don't exist in FileBlob returns not_found."""
        chunk1 = "a" * 40
        chunk2 = "b" * 40
        data = {
            "checksum": chunk1,
            "chunks": [chunk1, chunk2],
            "projects": [],
        }
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        result = res.json()
        self.assertEqual(result["state"], "not_found")
        self.assertCountEqual(result["missingChunks"], [chunk1, chunk2])

    def test_assemble_partial_chunks(self):
        """When some chunks exist, only missing ones are returned."""
        existing_checksum = "a" * 40
        missing_checksum = "b" * 40
        baker.make("files.FileBlob", checksum=existing_checksum)
        data = {
            "checksum": existing_checksum,
            "chunks": [existing_checksum, missing_checksum],
            "projects": [],
        }
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        result = res.json()
        self.assertEqual(result["state"], "not_found")
        self.assertEqual(result["missingChunks"], [missing_checksum])

    def test_assemble_all_chunks_present(self):
        """When all chunks exist, state is created with empty missingChunks."""
        chunk1 = "a" * 40
        chunk2 = "b" * 40
        baker.make("files.FileBlob", checksum=chunk1)
        baker.make("files.FileBlob", checksum=chunk2)
        version = "app@v1"
        baker.make(
            "releases.Release", version=version, organization=self.organization
        )
        data = {
            "checksum": chunk1,
            "chunks": [chunk1, chunk2],
            "projects": [],
            "version": version,
        }
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        result = res.json()
        self.assertEqual(result["state"], "created")
        self.assertEqual(result["missingChunks"], [])
