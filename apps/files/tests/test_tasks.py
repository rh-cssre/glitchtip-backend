from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import File, FileBlob
from ..tasks import cleanup_old_files
from .test_api import generate_file


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

        with freeze_time(now() + timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)):
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

        with freeze_time(now() + timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)):
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 0)
        self.assertEqual(File.objects.count(), 0)
