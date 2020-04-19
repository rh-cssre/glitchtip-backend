from datetime import timedelta
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from freezegun import freeze_time
from glitchtip import test_utils  # pylint: disable=unused-import
from .tasks import process_alerts
from .models import Notification, ProjectAlert


class AlertTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(user)
        team = baker.make("teams.Team", organization=self.organization)
        team.members.add(user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(team)

    def test_alerts(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=10,
        )

        issue = baker.make("issues.Issue", project=self.project)
        baker.make("issues.Event", issue=issue)

        # Not sufficient events to create alert
        process_alerts()
        self.assertEqual(Notification.objects.count(), 0)

        baker.make("issues.Event", issue=issue, _quantity=9)

        process_alerts()
        self.assertEqual(Notification.objects.count(), 1)

        # Notifications have a cooldown time equal to alert timespan
        process_alerts()
        self.assertEqual(Notification.objects.count(), 1)

        # Notifications should not happen again for same issue
        with freeze_time(self.now + timedelta(minutes=11)):
            baker.make("issues.Event", issue=issue, _quantity=10)
            process_alerts()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_alert_timing(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=10,
        )
        issue = baker.make("issues.Issue", project=self.project)

        # time 0: 4 events
        # time 5: 4 more events (8 total)
        # time 11: 4 more events (12 total)
        baker.make("issues.Event", issue=issue, _quantity=4)
        with freeze_time(self.now + timedelta(minutes=5)):
            baker.make("issues.Event", issue=issue, _quantity=4)
            process_alerts()
        with freeze_time(self.now + timedelta(minutes=11)):
            baker.make("issues.Event", issue=issue, _quantity=4)
            process_alerts()

        # Not sufficient rate of events to trigger alert.
        self.assertEqual(Notification.objects.count(), 0)

        # time 12: 4 more events (16 total, 12 in past 10 minutes)
        with freeze_time(self.now + timedelta(minutes=12)):
            baker.make("issues.Event", issue=issue, _quantity=4)
            process_alerts()
        self.assertEqual(Notification.objects.count(), 1)

    def test_alert_one_event(self):
        """ Use same logic to send alert for every new issue """
        baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=1, quantity=1,
        )
        issue = baker.make("issues.Issue", project=self.project)
        baker.make("issues.Event", issue=issue)
        process_alerts()
        self.assertEqual(Notification.objects.count(), 1)


class AlertAPITestCase(APITestCase):
    def setUp(self):
        user = baker.make("users.user")
        self.client.force_login(user)
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(user)
        team = baker.make("teams.Team", organization=self.organization)
        team.members.add(user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(team)

    def test_project_alerts_retrieve(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "project-alerts-list",
            kwargs={"project_pk": f"{self.organization.slug}/{self.project.slug}",},
        )
        res = self.client.get(url)
        self.assertContains(res, alert.timespan_minutes)

    def test_project_alerts_create(self):
        url = reverse(
            "project-alerts-list",
            kwargs={"project_pk": f"{self.organization.slug}/{self.project.slug}",},
        )
        data = {"timespan_minutes": 60, "quantity": 2}
        res = self.client.post(url, data)
        project_alert = ProjectAlert.objects.all().first()
        self.assertEqual(project_alert.timespan_minutes, data["timespan_minutes"])
        self.assertEqual(project_alert.project, self.project)

    def test_project_alerts_update(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "project-alerts-detail",
            kwargs={
                "project_pk": f"{self.organization.slug}/{self.project.slug}",
                "pk": alert.pk,
            },
        )
        data = {"timespan_minutes": 500, "quantity": 2}
        res = self.client.put(url, data)
        self.assertEqual(res.status_code, 200)
        project_alert = ProjectAlert.objects.all().first()
        self.assertEqual(project_alert.timespan_minutes, data["timespan_minutes"])
