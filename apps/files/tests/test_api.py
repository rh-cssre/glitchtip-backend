import os
from io import BytesIO
from urllib.parse import urlparse

from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.sourcecode.models import DebugSymbolBundle
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..models import File, FileBlob


def generate_file():
    im_io = BytesIO()
    return InMemoryUploadedFile(
        im_io, None, "random-name.jpg", "image/jpeg", len(im_io.getvalue()), None
    )


class ChunkUploadAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.url = reverse("api:get_chunk_upload_info", args=[self.organization.slug])

    def test_get(self):
        res = self.client.get(self.url)
        self.assertContains(res, self.organization.slug)

    def test_get_returns_relative_url_when_use_relative_url_is_true(self):
        """Chunk upload info URL is relative when GLITCHTIP_CHUNK_UPLOAD_ABSOLUTE_URL_PREFIX is unset."""
        with override_settings(GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL=True):
            res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        expected_path = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        self.assertEqual(data["url"], expected_path)
        self.assertTrue(data["url"].startswith("/"))

    def test_get_returns_absolute_url_when_use_relative_url_is_false(self):
        """Chunk upload info URL uses GLITCHTIP_URL when GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL is False."""
        base_url = urlparse("https://uploads.example.com")
        with override_settings(
            GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL=False,
            GLITCHTIP_URL=base_url,
        ):
            res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        expected_path = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        self.assertEqual(data["url"], base_url.geturl() + expected_path)
        self.assertTrue(data["url"].startswith("https://"))

    def test_post(self):
        data = {"file_gzip": generate_file()}
        res = self.client.post(self.url, data)
        self.assertEqual(res.status_code, 200)
        res = self.client.post(self.url, data)  # Should do nothing
        self.assertEqual(FileBlob.objects.count(), 1)


class ReleaseAssembleAPITests(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.organization.slug = "whab"
        self.organization.save()
        self.release = baker.make(
            "releases.Release", version="lol", organization=self.organization
        )
        self.url = reverse(
            "api:assemble_release", args=[self.organization.slug, self.release.version]
        )

    def test_post(self):
        checksum = "e56191dcd7d54035f26f7dec999de2b1e4f10129"
        filename = "runtime-es2015.456e9ca9da400255beb4.js"
        map_filename = filename + ".map"
        zip_file = SimpleUploadedFile(
            checksum,
            open(os.path.dirname(__file__) + "/test_zip/" + checksum, "rb").read(),
        )
        FileBlob.objects.create(blob=zip_file, size=3635, checksum=checksum)
        res = self.client.post(
            self.url,
            {"checksum": checksum, "chunks": [checksum]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(File.objects.get(name=filename))
        map_file = File.objects.get(name=map_filename)
        self.assertTrue(map_file)
        self.assertTrue(
            DebugSymbolBundle.objects.filter(
                sourcemap_file=map_file, release=self.release
            ).exists()
        )
