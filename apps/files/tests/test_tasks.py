from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.urls import reverse
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..maintenance import cleanup_old_files as _cleanup_old_files
from ..models import File, FileBlob
from .test_api import generate_file

cleanup_old_files = async_to_sync(_cleanup_old_files)


class TasksTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse("api:chunk_upload", args=[self.organization.slug])

    def test_cleanup_old_files(self):
        file = generate_file()
        data = {"file_gzip": file}
        self.client.post(self.url, data)
        file_blob = FileBlob.objects.first()
        release = baker.make("releases.Release", organization=self.organization)
        release_file = baker.make(
            "sourcecode.DebugSymbolBundle",
            file__blob=file_blob,
            organization=self.organization,
            release=release,
        )

        cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertEqual(File.objects.count(), 1)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)
        ):
            release_file = baker.make(
                "sourcecode.DebugSymbolBundle",
                file__blob=file_blob,
                organization=self.organization,
                release=release,
            )
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertEqual(File.objects.count(), 2)
        release_file.file.delete()

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)
        ):
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 0)
        self.assertEqual(File.objects.count(), 0)

    def test_cleanup_orphaned_file_blobs(self):
        file = generate_file()
        data = {"file_gzip": file}
        self.client.post(self.url, data)
        self.assertEqual(FileBlob.objects.count(), 1)
        blob = FileBlob.objects.first().blob

        # Orphaned blob younger than 24h is kept
        cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertTrue(blob.storage.exists(blob.name))

        # Orphaned blob older than 24h is deleted
        with freeze_time(now() + timedelta(hours=25)):
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 0)
        self.assertFalse(blob.storage.exists(blob.name))

    def test_cleanup_orphaned_file_blobs_keeps_referenced(self):
        file = generate_file()
        data = {"file_gzip": file}
        self.client.post(self.url, data)
        file_blob = FileBlob.objects.first()
        baker.make(File, blob=file_blob)

        with freeze_time(now() + timedelta(hours=25)):
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        blob = file_blob.blob
        self.assertTrue(blob.storage.exists(blob.name))
