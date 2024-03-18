from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import File, FileBlob
from ..tasks import cleanup_old_files
from .test_views import generate_file


class TasksTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.url = reverse(
            "chunk-upload", kwargs={"organization_slug": self.organization}
        )

    def test_cleanup_old_files(self):
        file = generate_file()
        data = {"file_gzip": file}
        self.client.post(self.url, data)
        file_blob = FileBlob.objects.first()
        release_file = baker.make(
            "releases.ReleaseFile",
            file__blob=file_blob,
            project__organization=self.organization,
        )

        cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertEqual(File.objects.count(), 1)

        with freeze_time(now() + timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)):
            release_file = baker.make(
                "releases.ReleaseFile",
                file__blob=file_blob,
                project__organization=self.organization,
            )
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 1)
        self.assertEqual(File.objects.count(), 2)
        release_file.file.delete()

        with freeze_time(now() + timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)):
            cleanup_old_files()
        self.assertEqual(FileBlob.objects.count(), 0)
        self.assertEqual(File.objects.count(), 0)
