from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..models import ProjectAlert


class AlertAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()

    def test_project_alerts_list(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )

        # Should not show up
        baker.make("alerts.ProjectAlert", timespan_minutes=60)
        # Second team could cause duplicates
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(self.org_user)
        self.project.teams.add(team2)

        url = reverse(
            "api:list_project_alerts", args=[self.organization.slug, self.project.slug]
        )
        res = self.client.get(url)
        self.assertContains(res, alert.id)
        self.assertEqual(len(res.json()), 1)

    def test_project_alerts_create(self):
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        # Test all supported recipient types and tagsToAdd
        recipients = [
            {"recipientType": "email", "url": "", "tagsToAdd": ["tag1"]},
            {
                "recipientType": "discord",
                "url": "https://discord.com/api/webhooks/123",
                "tagsToAdd": ["tag2"],
            },
            {
                "recipientType": "webhook",
                "url": "https://example.com/webhook",
                "tagsToAdd": [],
            },
            {
                "recipientType": "googlechat",
                "url": "https://chat.googleapis.com/webhook/abc",
                "tagsToAdd": ["tag3"],
            },
        ]
        data = {
            "name": "foo",
            "timespanMinutes": 60,
            "quantity": 2,
            "uptime": True,
            "alertRecipients": recipients,
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        project_alert = ProjectAlert.objects.filter(name="foo", uptime=True).first()
        self.assertEqual(project_alert.timespan_minutes, data["timespanMinutes"])
        self.assertEqual(project_alert.project, self.project)
        # Check that all recipients were created
        self.assertEqual(project_alert.alertrecipient_set.count(), 4)
        for i, recipient in enumerate(project_alert.alertrecipient_set.all()):
            self.assertEqual(recipient.tags_to_add, recipients[i]["tagsToAdd"])

    def test_project_alerts_create_invalid_recipient_type(self):
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        data = {
            "name": "foo",
            "timespanMinutes": 60,
            "quantity": 2,
            "uptime": True,
            "alertRecipients": [{"recipientType": "invalid", "url": ""}],
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    def test_project_alerts_update_all_types(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        recipients = [
            {
                "recipientType": "discord",
                "url": "https://discord.com/api/webhooks/123",
                "tagsToAdd": ["tag2"],
            },
            {
                "recipientType": "webhook",
                "url": "https://example.com/webhook",
                "tagsToAdd": [],
            },
            {
                "recipientType": "googlechat",
                "url": "https://chat.googleapis.com/webhook/abc",
                "tagsToAdd": ["tag3"],
            },
        ]
        data = {
            "timespanMinutes": 500,
            "quantity": 2,
            "alertRecipients": recipients,
        }
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.alertrecipient_set.count(), 3)
        for i, recipient in enumerate(alert.alertrecipient_set.all()):
            self.assertEqual(recipient.tags_to_add, recipients[i]["tagsToAdd"])

    def test_project_alerts_create_permissions(self):
        user = baker.make("users.user")
        org_user = self.organization.add_user(user, OrganizationUserRole.MEMBER)

        self.client.force_login(user)
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        data = {
            "name": "foo",
            "timespanMinutes": 60,
            "quantity": 2,
            "uptime": True,
            "alertRecipients": [{"recipientType": "email", "url": ""}],
        }
        res = self.client.post(url, data, content_type="application/json")
        # Member without project team membership cannot create alerts
        self.assertEqual(res.status_code, 404)

        org_user.role = OrganizationUserRole.ADMIN
        org_user.save()
        # Add second team to ensure we don't get MultipleObjectsReturned
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(org_user)
        self.project.teams.add(team2)

        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

        org_user.role = OrganizationUserRole.MEMBER
        org_user.save()
        res = self.client.get(url)
        # Members can still view alerts
        self.assertEqual(len(res.json()), 1)

    def test_project_alerts_update(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )

        data = {
            "timespanMinutes": 500,
            "quantity": 2,
            "alertRecipients": [
                {"recipientType": "discord", "url": "https://example.com"},
            ],
        }
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, data["alertRecipients"][0]["url"])

        # Webhooks require url
        data = {
            "alertRecipients": [
                {"recipientType": "discord", "url": ""},
            ],
        }
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    def test_project_alerts_update_auth(self):
        """Cannot update alert on project that user does not belong to"""
        alert = baker.make("alerts.ProjectAlert", timespan_minutes=60)
        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        data = {"timespanMinutes": 500, "quantity": 2}
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 404)

    def test_project_alerts_delete(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:delete_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = self.client.delete(url, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(ProjectAlert.objects.count(), 0)

    def test_delete_with_second_team(self):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:delete_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(self.org_user)
        self.project.teams.add(team2)
        res = self.client.delete(url, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(ProjectAlert.objects.count(), 0)
