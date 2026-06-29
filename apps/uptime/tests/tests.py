from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from aioresponses import aioresponses
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core import mail
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import ProjectAlertStatus
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import MonitorType
from ..models import Monitor, MonitorCheck
from ..tasks import dispatch_checks, save_monitor_checks
from ..utils import fetch_all
from ..webhooks import send_uptime_as_webhook


class UptimeTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from glitchtip.partition_manager import PartitionManager

        # Create partitions for 2020-01-01 to 2020-01-07 to cover test data
        manager = PartitionManager()
        start_date = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)
        end_date = start_date + timedelta(days=7)
        manager.create_partitions_for_date_range(
            parent_table="uptime_monitorcheck",
            start_date=start_date,
            end_date=end_date,
            partition_interval="DAY",
            key_type="uuid7",
        )

    def create_user_and_project(self):
        self.create_logged_in_user()

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_dispatch_checks(self, mocked):
        mocked.aenqueue = mock.AsyncMock()
        test_url = "https://example.com"
        with freeze_time("2020-01-01"):
            mon1 = baker.make(
                Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60
            )
            baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60)
            baker.make(MonitorCheck, monitor=mon1, organization=mon1.organization)

        # Run through a full interval to ensure we hit the monitors
        async def run_loop():
            for _ in range(60):
                await dispatch_checks.func()

        async_to_sync(run_loop)()

        self.assertGreaterEqual(mocked.aenqueue.call_count, 1)

    @aioresponses()
    def test_fetch_all(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        mon1 = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
        mocked.get(test_url, status=200)
        monitors = list(Monitor.objects.all().values())
        results = async_to_sync(fetch_all)(monitors)
        self.assertEqual(results[0]["id"], mon1.pk)

    @aioresponses()
    def test_monitor_checks_integration(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60
            )
        self.assertEqual(mon.checks.count(), 1)

        mocked.get(test_url, status=200, repeat=True)
        with freeze_time("2020-01-01"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertEqual(mon.checks.count(), 2)

        # Ensure it runs again in the next interval
        with freeze_time("2020-01-02"):
            async_to_sync(run_loop)()
        self.assertEqual(mon.checks.count(), 3)

    def test_is_change_baseline_after_pruning(self):
        """When partition retention prunes all is_change=True records,
        the next check should be marked is_change=True to establish a baseline."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )
        # Simulate post-pruning: only is_change=False checks remain
        with freeze_time("2020-01-01"):
            baker.make(
                MonitorCheck,
                monitor=mon,
                organization=mon.organization,
                is_up=True,
                is_change=False,
            )

        # Run save_monitor_checks with a result that matches current state
        # (is_up=True, latest_is_up=True, last_change=None)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": True,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], timezone.now())

        # The new check should have is_change=True (baseline)
        latest_check = mon.checks.order_by("-start_check").first()
        self.assertTrue(latest_check.is_change)

    @aioresponses()
    def test_expected_response(self, mocked):
        test_url = "https://example.com"

        mocked.get(test_url, status=200, body="Status: OK")
        monitor = baker.make(
            Monitor,
            name=test_url,
            url=test_url,
            expected_body="OK",
            monitor_type=MonitorType.GET,
        )
        check = monitor.checks.first()
        self.assertTrue(check.is_up)

        mocked.get(test_url, status=200, body="Status: Failure")
        monitor = baker.make(
            Monitor,
            name=test_url,
            url=test_url,
            expected_body="OK",
            monitor_type=MonitorType.GET,
        )
        check = monitor.checks.first()
        self.assertFalse(check.is_up)
        self.assertEqual(check.data["payload"], "Status: Failure")

    @aioresponses()
    def test_monitor_notifications(self, mocked):
        self.create_user_and_project()
        test_url = "https://example.com"
        webhook_url = "https://webhook.example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="webhook",
                url=webhook_url,
            )

        mocked.get(test_url, status=500)
        mocked.post(webhook_url, status=200)

        # We need to hit the tick that matches the monitor ID
        async def run_loop():
            for _ in range(60):
                await dispatch_checks.func()

        with freeze_time("2020-01-02"):
            async_to_sync(run_loop)()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("is down", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].extra_headers["X-Mailer"], "GlitchTip")
        self.assertEqual(
            mail.outbox[0].extra_headers["X-GlitchTip-Project"], self.project.name
        )
        self.assertEqual(
            mail.outbox[0].extra_headers["X-GlitchTip-Organization"],
            self.project.organization.name,
        )
        self.assertEqual(
            mail.outbox[0].extra_headers["List-Id"],
            f"<{self.project.slug}.{self.project.organization.slug}.{settings.GLITCHTIP_URL.hostname}>",
        )

        mocked.get(test_url, status=500)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)

        mocked.get(test_url, status=200)
        mocked.post(webhook_url, status=200)
        with freeze_time("2020-01-04"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("is back up", mail.outbox[1].body)

    @mock.patch("aiohttp.ClientSession")
    def test_discord_webhook(self, MockSession):
        from apps.alerts.tests.test_webhooks import _mock_aiohttp_session

        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        self.create_user_and_project()
        test_url = "https://example.com"
        webhook_url = "https://discord.com/api/webhooks/test/test"
        check = baker.make(
            "uptime.MonitorCheck",
            monitor__monitor_type=MonitorType.GET,
            monitor__url=test_url,
            monitor__project=self.project,
        )
        recipient = baker.make(
            "alerts.AlertRecipient", recipient_type="discord", url=webhook_url
        )
        async_to_sync(send_uptime_as_webhook)(recipient, check.pk, True, timezone.now())
        mock_post.assert_called_once()

    @aioresponses()
    def test_notification_default_scope(self, mocked):
        """Subscribe by default should not result in alert emails for non-team members"""
        self.create_user_and_project()
        test_url = "https://example.com"

        # user2 is an org member but not in a relevant team, should not receive alerts
        user2 = baker.make("users.user")
        org_user2 = self.organization.add_user(user2, OrganizationUserRole.MEMBER)
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(org_user2)

        # user3 is in team3 which should receive alerts
        user3 = baker.make("users.user")
        org_user3 = self.organization.add_user(user3, OrganizationUserRole.MEMBER)
        self.team.members.add(org_user3)
        team3 = baker.make("teams.Team", organization=self.organization)
        team3.members.add(org_user3)
        team3.projects.add(self.project)

        baker.make(
            "alerts.AlertRecipient",
            alert__uptime=True,
            alert__project=self.project,
            recipient_type="email",
        )

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )

        mocked.get(test_url, status=500, repeat=True)
        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertNotIn(user2.email, mail.outbox[0].to)
        self.assertIn(user3.email, mail.outbox[0].to)
        self.assertEqual(len(mail.outbox[0].to), 2)

    @aioresponses()
    def test_user_project_alert_scope(self, mocked):
        """User project alert should not result in alert emails for non-team members"""
        self.create_user_and_project()
        test_url = "https://example.com"
        baker.make(
            "alerts.AlertRecipient",
            alert__uptime=True,
            alert__project=self.project,
            recipient_type="email",
        )

        user2 = baker.make("users.user")
        self.organization.add_user(user2, OrganizationUserRole.MEMBER)

        baker.make(
            "projects.UserProjectAlert",
            user=user2,
            project=self.project,
            status=ProjectAlertStatus.ON,
        )

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )

        mocked.get(test_url, status=500, repeat=True)
        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertNotIn(user2.email, mail.outbox[0].to)

    def xtest_heartbeat(self):
        """
        Cannot run due to async code, it doesn't close the DB connection
        Run manually with --keepdb
        """
        self.create_user_and_project()
        with freeze_time("2020-01-01"):
            monitor = baker.make(
                Monitor,
                monitor_type=MonitorType.HEARTBEAT,
                project=self.project,
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )
            url = reverse(
                "api:heartbeat_check",
                kwargs={
                    "organization_slug": monitor.organization.slug,
                    "endpoint_id": monitor.endpoint_id,
                },
            )
            self.assertFalse(monitor.checks.exists())
            self.client.post(url)
            self.assertTrue(monitor.checks.filter(is_up=True).exists())
            async_to_sync(dispatch_checks.func)()
        self.assertTrue(monitor.checks.filter(is_up=True).exists())
        self.assertEqual(len(mail.outbox), 0)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()  # Still down
        self.assertEqual(len(mail.outbox), 1)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-04"):
            self.client.post(url)  # Back up
        self.assertEqual(len(mail.outbox), 2)

    def test_heartbeat_grace_period(self):
        # Don't alert users when heartbeat check has never come in
        self.create_user_and_project()
        baker.make(Monitor, monitor_type=MonitorType.HEARTBEAT, project=self.project)
        async_to_sync(dispatch_checks.func)()
        self.assertEqual(len(mail.outbox), 0)

    def test_cached_fields_updated_on_save_monitor_checks(self):
        """cached_is_up and cached_last_change update after save_monitor_checks"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )
        self.assertIsNone(mon.cached_is_up)
        self.assertIsNone(mon.cached_last_change)

        now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, now)

    def test_cached_fields_updated_on_state_change(self):
        """cached_last_change updates when is_up state changes"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )

        first_now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], first_now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, first_now)

        # Now simulate going down
        second_now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        result2 = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": False,
            "latest_is_up": True,
            "last_change": first_now,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result2], second_now)

        mon.refresh_from_db()
        self.assertFalse(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, second_now)

    def test_cached_fields_preserved_when_no_change(self):
        """cached_last_change is preserved when is_up state doesn't change"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )

        first_now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], first_now)

        # Same state, no change
        second_now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        result2 = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": True,
            "last_change": first_now,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result2], second_now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        # last_change should stay at first_now since no state change
        self.assertEqual(mon.cached_last_change, first_now)

    @mock.patch("apps.uptime.utils.asyncio.open_connection")
    def test_port_monitor(self, mocked):
        self.create_user_and_project()
        monitor = baker.make(
            Monitor,
            url="example.com:80",
            monitor_type=MonitorType.PORT,
            project=self.project,
        )
        mocked.assert_called_once()
        self.assertTrue(monitor.checks.filter(is_up=True).exists())

    def _down_result(self, mon, consecutive_down, threshold):
        return {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": False,
            "latest_is_up": True,
            "last_change": datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
            "cached_consecutive_down": consecutive_down,
            "confirmation_threshold": threshold,
            "monitor_type": MonitorType.GET,
        }

    def test_confirmation_threshold_below_does_not_flip_state(self):
        """A failure below confirmation_threshold counts toward the consecutive
        tally but leaves the official state up and sends no notification."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                confirmation_threshold=2,
            )
        now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([self._down_result(mon, 0, 2)], now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)  # still officially up
        self.assertEqual(mon.cached_consecutive_down, 1)
        # The raw failing check is still recorded, but it isn't a state change
        latest = mon.checks.order_by("-start_check").first()
        self.assertFalse(latest.is_up)
        self.assertFalse(latest.is_change)

    def test_confirmation_threshold_reached_flips_state(self):
        """The check that reaches confirmation_threshold flips state to down."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                confirmation_threshold=2,
            )
        now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([self._down_result(mon, 1, 2)], now)

        mon.refresh_from_db()
        self.assertFalse(mon.cached_is_up)
        self.assertEqual(mon.cached_consecutive_down, 2)
        self.assertEqual(mon.cached_last_change, now)
        latest = mon.checks.order_by("-start_check").first()
        self.assertTrue(latest.is_change)

    def test_consecutive_down_resets_on_success(self):
        """A successful check resets the consecutive-failure tally to zero
        without recording a state change (state was never confirmed down)."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                confirmation_threshold=2,
                cached_is_up=True,
                cached_consecutive_down=1,
            )
        now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": True,
            "last_change": datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
            "cached_consecutive_down": 1,
            "confirmation_threshold": 2,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result], now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(mon.cached_consecutive_down, 0)

    def test_consecutive_down_capped_at_threshold(self):
        """A long-running outage keeps incrementing the tally; it must stop at
        confirmation_threshold so it never overflows the smallint column."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                confirmation_threshold=2,
                cached_is_up=False,
            )
        now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        # Already at the threshold and confirmed down; another failure must not
        # push the counter past the threshold.
        result = self._down_result(mon, 2, 2)
        result["latest_is_up"] = False
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result], now)

        mon.refresh_from_db()
        self.assertFalse(mon.cached_is_up)
        self.assertEqual(mon.cached_consecutive_down, 2)

    @aioresponses()
    def test_confirmation_threshold_delays_notification(self, mocked):
        """With confirmation_threshold=2, no notification fires on the first
        failure; the second consecutive failure sends a single notification."""
        self.create_user_and_project()
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
                confirmation_threshold=2,
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )

        async def run_loop():
            for _ in range(60):
                await dispatch_checks.func()

        # First failure: below threshold, no notification yet
        mocked.get(test_url, status=500, repeat=True)
        with freeze_time("2020-01-02"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 0)

        # Second consecutive failure: threshold reached, notify once
        mocked.get(test_url, status=500, repeat=True)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("is down", mail.outbox[0].body)
