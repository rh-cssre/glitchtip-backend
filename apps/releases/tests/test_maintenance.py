from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import IntegrityError
from django.db.models import QuerySet
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

    def test_cleanup_integrity_error_does_not_loop_forever(self):
        """
        When _raw_delete raises IntegrityError (concurrent FK insert),
        cleanup must break rather than re-selecting the same undeletable
        batch in an infinite loop.
        """
        now = timezone.now()
        old = baker.make("releases.Release", organization=self.organization)
        Release.objects.filter(id=old.id).update(
            created=now - timedelta(days=settings.GLITCHTIP_RELEASE_RETENTION_DAYS)
        )

        release_delete_count = 0
        real_raw_delete = QuerySet._raw_delete

        def patched_raw_delete(self, using):
            nonlocal release_delete_count
            if self.model is Release:
                release_delete_count += 1
                if release_delete_count > 3:
                    raise AssertionError(
                        "Infinite loop: Release._raw_delete called >3 times"
                    )
                raise IntegrityError("simulated concurrent FK insert")
            return real_raw_delete(self, using)

        with patch.object(QuerySet, "_raw_delete", patched_raw_delete):
            cleanup_old_releases()

        # Should have been called exactly once, then break
        self.assertEqual(release_delete_count, 1)
        # Release still exists (couldn't be deleted)
        self.assertTrue(Release.objects.filter(id=old.id).exists())
