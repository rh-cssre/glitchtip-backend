import os
import tempfile
from hashlib import sha1
from io import BytesIO
from urllib.parse import urlparse

from django.core.files.base import File as DjangoFile
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
        self.async_client.force_login(self.user)
        self.url = reverse("api:get_chunk_upload_info", args=[self.organization.slug])

    async def test_get(self):
        res = await self.async_client.get(self.url)
        self.assertContains(res, self.organization.slug)

    async def test_get_returns_relative_url_when_use_relative_url_is_true(self):
        """Chunk upload info URL is relative when GLITCHTIP_CHUNK_UPLOAD_ABSOLUTE_URL_PREFIX is unset."""
        with override_settings(GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL=True):
            res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        expected_path = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        self.assertEqual(data["url"], expected_path)
        self.assertTrue(data["url"].startswith("/"))

    async def test_get_returns_absolute_url_when_use_relative_url_is_false(self):
        """Chunk upload info URL uses GLITCHTIP_URL when GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL is False."""
        base_url = urlparse("https://uploads.example.com")
        with override_settings(
            GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL=False,
            GLITCHTIP_URL=base_url,
        ):
            res = await self.async_client.get(self.url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        expected_path = reverse(
            "api:get_chunk_upload_info", args=[self.organization.slug]
        )
        self.assertEqual(data["url"], base_url.geturl() + expected_path)
        self.assertTrue(data["url"].startswith("https://"))

    async def test_post(self):
        data = {"file_gzip": generate_file()}
        res = await self.async_client.post(self.url, data)
        self.assertEqual(res.status_code, 200)
        res = await self.async_client.post(self.url, data)  # Should do nothing
        self.assertEqual(await FileBlob.objects.acount(), 1)


class ReleaseAssembleAPITests(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)
        self.organization.slug = "whab"
        self.organization.save()
        self.release = baker.make(
            "releases.Release", version="lol", organization=self.organization
        )
        self.url = reverse(
            "api:assemble_release", args=[self.organization.slug, self.release.version]
        )

    async def test_post(self):
        checksum = "e56191dcd7d54035f26f7dec999de2b1e4f10129"
        filename = "runtime-es2015.456e9ca9da400255beb4.js"
        map_filename = filename + ".map"
        zip_file = SimpleUploadedFile(
            checksum,
            open(os.path.dirname(__file__) + "/test_zip/" + checksum, "rb").read(),
        )
        await FileBlob.objects.acreate(blob=zip_file, size=3635, checksum=checksum)
        res = await self.async_client.post(
            self.url,
            {"checksum": checksum, "chunks": [checksum]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(await File.objects.aget(name=filename))
        map_file = await File.objects.aget(name=map_filename)
        self.assertTrue(map_file)
        self.assertTrue(
            await DebugSymbolBundle.objects.filter(
                sourcemap_file=map_file, release=self.release
            ).aexists()
        )


class AssembleFromFileBlobIdsTests(TestCase):
    @staticmethod
    def create_file_blob(content: bytes) -> FileBlob:
        checksum = sha1(content).hexdigest()
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(content)
        tmp.flush()
        tmp.seek(0)
        blob, _ = FileBlob.objects.get_or_create(
            checksum=checksum,
            defaults={"blob": DjangoFile(tmp, name=checksum), "size": len(content)},
        )
        tmp.close()
        return blob

    def test_single_chunk_reuses_blob(self):
        blob = self.create_file_blob(b"solo")
        checksum = blob.checksum
        file = File.objects.create(name="solo.txt", checksum="")
        file.assemble_from_file_blob_ids([blob.id], checksum)
        self.assertEqual(file.blob_id, blob.id)

    def test_multi_chunk_concatenates_into_combined_blob(self):
        blob1 = self.create_file_blob(b"aaa")
        blob2 = self.create_file_blob(b"bbb")
        checksum = sha1(b"aaabbb").hexdigest()
        file = File.objects.create(name="multi.txt", checksum="")
        tf = file.assemble_from_file_blob_ids([blob1.id, blob2.id], checksum)
        self.assertEqual(file.size, 6)
        self.assertEqual(file.checksum, checksum)
        self.assertEqual(file.blob.checksum, checksum)
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"aaabbb")
        self.assertEqual(tf.read(), b"aaabbb")
        tf.close()

    def test_multi_chunk_respects_order(self):
        blob1 = self.create_file_blob(b"aaa")
        blob2 = self.create_file_blob(b"bbb")
        checksum = sha1(b"bbbaaa").hexdigest()
        file = File.objects.create(name="reversed.txt", checksum="")
        tf = file.assemble_from_file_blob_ids([blob2.id, blob1.id], checksum)
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"bbbaaa")
        tf.close()

    def test_duplicate_chunk(self):
        blob = self.create_file_blob(b"aa")
        checksum = sha1(b"aaaa").hexdigest()
        file = File.objects.create(name="dup.txt", checksum="")
        tf = file.assemble_from_file_blob_ids([blob.id, blob.id], checksum)
        self.assertEqual(file.size, 4)
        with file.blob.blob.open("rb") as f:
            self.assertEqual(f.read(), b"aaaa")
        tf.close()
