import asyncio
from unittest import mock
from aioresponses import aioresponses
from django.test import TestCase
from freezegun import freeze_time
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from .tasks import dispatch_checks
from .utils import fetch_all
from .models import Monitor, MonitorCheck
from .constants import MonitorType


class UptimeTestCase(TestCase):
    @mock.patch("uptime.tasks.perform_checks.run")
    def test_dispatch_checks(self, mocked):
        mock.return_value = None
        test_url = "https://example.com"
        with freeze_time("2020-01-01"):
            mon1 = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
            mon2 = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
            baker.make(MonitorCheck, monitor=mon1)

        self.assertEqual(mocked.call_count, 2)
        with freeze_time("2020-01-02"):
            baker.make(MonitorCheck, monitor=mon2)
            dispatch_checks()
        self.assertEqual(mocked.call_count, 3)

    @aioresponses()
    def test_fetch_all(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        mon1 = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
        mocked.get(test_url, status=200)
        loop = asyncio.get_event_loop()
        monitors = list(Monitor.objects.all().values())
        results = loop.run_until_complete(fetch_all(monitors, loop))
        self.assertEqual(results[0]["id"], mon1.pk)

    @aioresponses()
    def test_monitor_checks_integration(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            mon = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
        self.assertEqual(mon.checks.count(), 1)

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            dispatch_checks()
        self.assertEqual(mon.checks.count(), 1)

        with freeze_time("2020-01-02"):
            with self.assertNumQueries(3):
                dispatch_checks()
        self.assertEqual(mon.checks.count(), 2)
