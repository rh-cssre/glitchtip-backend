from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import MonitorCheck
from ..tasks import cleanup_old_monitor_checks


class TasksTestCase(GlitchTipTestCase):
    @mock.patch("glitchtip.uptime.tasks.perform_checks.run")
    def test_cleanup_old_monitor_checks(self, _):
        baker.make(MonitorCheck, _quantity=2)
        cleanup_old_monitor_checks()
        self.assertEqual(MonitorCheck.objects.count(), 2)

        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_monitor_checks()
        self.assertEqual(MonitorCheck.objects.count(), 0)
