from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase

from ..maintenance import cleanup_old_releases as _cleanup_old_releases
from ..models import Deploy, Release

cleanup_old_releases = async_to_sync(_cleanup_old_releases)


class ReleaseMaintenanceTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def test_cleanup_old_releases(self):
        now = timezone.now()
        recent = baker.make("releases.Release", organization=self.organization)
        old = baker.make("releases.Release", organization=self.organization)
        Release.objects.filter(id=old.id).update(
            created=now - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
        )
        baker.make("releases.Deploy", release=old)

        cleanup_old_releases()

        self.assertEqual(Release.objects.count(), 1)
        self.assertEqual(Release.objects.first().id, recent.id)
        self.assertEqual(Deploy.objects.count(), 0)
