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
from ..tasks import dispatch_checks
from ..utils import fetch_all
from ..webhooks import send_uptime_as_webhook


class UptimeTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
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
            baker.make(MonitorCheck, monitor=mon1)

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
    @mock.patch("requests.post")
    def test_monitor_notifications(self, mocked, mock_post):
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
                url="https://example.com",
            )

        mocked.get(test_url, status=500)

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

        mock_post.assert_called_once()

        mocked.get(test_url, status=500)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-04"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("is back up", mail.outbox[1].body)

    @aioresponses()
    @mock.patch("requests.post")
    def test_discord_webhook(self, mocked, mocked_post):
        self.create_user_and_project()
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        check = baker.make(
            "uptime.MonitorCheck",
            monitor__monitor_type=MonitorType.GET,
            monitor__url=test_url,
            monitor__project=self.project,
        )
        recipient = baker.make("alerts.AlertRecipient", recipient_type="discord")
        send_uptime_as_webhook(recipient, check.pk, True, timezone.now())
        mocked_post.assert_called_once()

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
