import json
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.issue_events.models import EventStatus, Issue
from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import ProjectAlertStatus
from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import Notification
from ..tasks import process_event_alerts


class AlertTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.now = timezone.now()

    def test_alerts(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=10,
        )

        issue = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue)

        # Not sufficient events to create alert
        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 0)

        baker.make("issue_events.IssueEvent", issue=issue, _quantity=9)

        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)

        # Notifications have a cooldown time equal to alert timespan
        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)

        # Notifications should not happen again for same issue
        with freeze_time(self.now + timedelta(minutes=11)):
            baker.make("issue_events.IssueEvent", issue=issue, _quantity=10)
            process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_multiple_alerts(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=1,
        )
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=2,
        )

        issue1 = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue1)

        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)

        issue2 = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue2, _quantity=2)

        process_event_alerts()
        # Trigger both alerts, as both meet criteria, total of 3
        self.assertEqual(Notification.objects.count(), 3)

    def test_alert_timing(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=10,
        )
        issue = baker.make("issue_events.Issue", project=self.project)

        # time 0: 4 events
        # time 5: 4 more events (8 total)
        # time 11: 4 more events (12 total)
        baker.make("issue_events.IssueEvent", issue=issue, _quantity=4)
        with freeze_time(self.now + timedelta(minutes=5)):
            baker.make("issue_events.IssueEvent", issue=issue, _quantity=4)
            process_event_alerts()
        with freeze_time(self.now + timedelta(minutes=11)):
            baker.make("issue_events.IssueEvent", issue=issue, _quantity=4)
            process_event_alerts()

        # Not sufficient rate of events to trigger alert.
        self.assertEqual(Notification.objects.count(), 0)

        # time 12: 4 more events (16 total, 12 in past 10 minutes)
        with freeze_time(self.now + timedelta(minutes=12)):
            baker.make("issue_events.IssueEvent", issue=issue, _quantity=4)
            process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)

    def test_alert_one_event(self):
        """Use same logic to send alert for every new issue"""
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )
        issue = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue)
        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 1)

    def test_alert_on_regression(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )

        # Make event
        with open(
            "events/test_data/incoming_events/very_small_event.json"
        ) as json_file:
            data = json.load(json_file)
        projectkey = self.project.projectkey_set.first()
        params = f"?sentry_key={projectkey.public_key}"
        url = reverse("api:event_store", args=[self.project.id]) + params
        self.client.post(url, data, content_type="application/json")

        # First alert
        process_event_alerts()
        self.assertEqual(len(mail.outbox), 1)

        # Mark resolved
        issue = Issue.objects.first()
        issue.status = EventStatus.RESOLVED
        issue.save()

        # Send a second event
        data["event_id"] = "cf536c31b68a473f97e579507ce155e4"
        self.client.post(url, data, content_type="application/json")
        process_event_alerts()
        self.assertEqual(len(mail.outbox), 2)

    def test_alert_subscription_default_scope(self):
        """Subscribe by default should not result in alert emails for non-team members"""
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )

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

        baker.make("issue_events.IssueEvent", issue__project=self.project)
        process_event_alerts()
        self.assertNotIn(user2.email, mail.outbox[0].to)
        self.assertIn(user3.email, mail.outbox[0].to)
        self.assertEqual(len(mail.outbox[0].to), 2)  # Ensure no duplicate emails

    def test_alert_queries(self):
        project2 = baker.make("projects.Project", organization=self.organization)
        project2.teams.add(self.team)
        project3 = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )
        baker.make(
            "alerts.ProjectAlert",
            project=project2,
            timespan_minutes=1,
            quantity=1,
        )
        baker.make(
            "alerts.ProjectAlert",
            project=project2,
            timespan_minutes=1,
            quantity=2,
        )
        baker.make(
            "alerts.ProjectAlert",
            project=project3,
            timespan_minutes=1,
            quantity=1,
        )
        with self.assertNumQueries(5):
            process_event_alerts()

    def test_email_headers(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )
        issue = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue)
        process_event_alerts()

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]

        self.assertEqual(email.extra_headers["X-Mailer"], "GlitchTip")
        self.assertEqual(email.extra_headers["X-GlitchTip-Project"], self.project.name)
        self.assertEqual(
            email.extra_headers["X-GlitchTip-Organization"],
            self.project.organization.name,
        )
        self.assertEqual(
            email.extra_headers["List-Id"],
            f"<{self.project.slug}.{self.project.organization.slug}.{settings.GLITCHTIP_URL.hostname}>",
        )


class AlertWithUserProjectAlert(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()
        self.now = timezone.now()

    def test_alert_enabled_user_project_alert_disabled(self):
        """A user should be able to disable their own notifications"""
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )
        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=self.project,
            status=ProjectAlertStatus.OFF,
        )

        user2 = baker.make("users.user")
        org_user2 = self.organization.add_user(user2, OrganizationUserRole.ADMIN)
        self.team.members.add(org_user2)
        baker.make(
            "projects.UserProjectAlert",
            user=user2,
            status=ProjectAlertStatus.ON,
        )

        user3 = baker.make("users.user")
        org_user3 = self.organization.add_user(user3, OrganizationUserRole.ADMIN)
        self.team.members.add(org_user3)
        baker.make(
            "projects.UserProjectAlert",
            user=user3,
            project=self.project,
            status=ProjectAlertStatus.ON,
        )

        baker.make("issue_events.IssueEvent", issue__project=self.project)
        process_event_alerts()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].merge_metadata), 2)

    def test_alert_enabled_subscribe_by_default(self):
        self.user.subscribe_by_default = False
        self.user.save()
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )

        baker.make("issue_events.IssueEvent", issue__project=self.project)
        process_event_alerts()
        self.assertEqual(len(mail.outbox), 0)

    def test_alert_enabled_subscribe_by_default_override_false(self):
        self.user.subscribe_by_default = False
        self.user.save()
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )
        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=self.project,
            status=ProjectAlertStatus.ON,
        )
        baker.make("issue_events.IssueEvent", issue__project=self.project)
        process_event_alerts()
        self.assertEqual(len(mail.outbox), 1)

    def test_user_project_alert_scope(self):
        """User project alert should not result in alert emails for non-team members"""
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=1,
            quantity=1,
        )

        user2 = baker.make("users.user")
        self.organization.add_user(user2, OrganizationUserRole.MEMBER)

        baker.make(
            "projects.UserProjectAlert",
            user=user2,
            project=self.project,
            status=ProjectAlertStatus.ON,
        )
        baker.make("issue_events.IssueEvent", issue__project=self.project)
        process_event_alerts()
        self.assertNotIn(user2.email, mail.outbox[0].to)
