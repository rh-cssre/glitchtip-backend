from unittest import mock

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import RecipientType
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
            {
                "recipientType": "teams",
                "url": "https://example.webhook.office.com/webhookb2/test",
                "tagsToAdd": [],
            },
            {
                "recipientType": "zulip",
                "url": "https://zulip.example.com",
                "botEmail": "bot@zulip.example.com",
                "apiKey": "test-api-key",
                "channel": "alerts",
                "topic": "GlitchTip Alerts",
                "tagsToAdd": [],
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
        self.assertEqual(project_alert.alertrecipient_set.count(), 6)
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
            {
                "recipientType": "zulip",
                "url": "https://zulip.example.com",
                "botEmail": "bot@zulip.example.com",
                "apiKey": "test-api-key",
                "channel": "alerts",
                "tagsToAdd": [],
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
        self.assertEqual(alert.alertrecipient_set.count(), 4)
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

    @mock.patch("requests.post")
    def test_test_project_alert(self, mock_post):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="https://example.com/webhook",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = self.client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["recipientType"], "webhook")
        self.assertEqual(data[0]["status"], "sent")
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_test_project_alert_skips_email(self, mock_post):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.EMAIL,
            url="",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = self.client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "skipped")
        mock_post.assert_not_called()

    @mock.patch(
        "apps.alerts.api.send_test_notification",
        side_effect=Exception("Connection refused"),
    )
    def test_test_project_alert_error(self, mock_send):
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.NTFY,
            url="https://ntfy.sh/test-topic",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = self.client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "error")
        self.assertEqual(data[0]["message"], "Connection refused")

    def test_project_alerts_create_zulip(self):
        """Zulip recipient stores config fields correctly."""
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        data = {
            "name": "zulip-test",
            "timespanMinutes": 60,
            "quantity": 2,
            "alertRecipients": [
                {
                    "recipientType": "zulip",
                    "url": "https://zulip.example.com",
                    "botEmail": "bot@zulip.example.com",
                    "apiKey": "test-api-key",
                    "channel": "alerts",
                    "topic": "GlitchTip Alerts",
                    "tagsToAdd": ["env"],
                }
            ],
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        alert = ProjectAlert.objects.get(name="zulip-test")
        recipient = alert.alertrecipient_set.first()
        self.assertEqual(recipient.recipient_type, "zulip")
        self.assertEqual(recipient.url, "https://zulip.example.com/")
        self.assertEqual(recipient.config["bot_email"], "bot@zulip.example.com")
        self.assertEqual(recipient.config["api_key"], "test-api-key")
        self.assertEqual(recipient.config["channel"], "alerts")
        self.assertEqual(recipient.config["topic"], "GlitchTip Alerts")
        self.assertEqual(recipient.tags_to_add, ["env"])
        # Verify config is returned in API response
        res_data = res.json()
        zulip_recipient = res_data["alertRecipients"][0]
        self.assertEqual(
            zulip_recipient["config"]["bot_email"], "bot@zulip.example.com"
        )

    def test_project_alerts_update_zulip_config(self):
        """Updating a Zulip recipient's config (e.g. rotating API key) works."""
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.ZULIP,
            url="https://zulip.example.com/",
            config={
                "bot_email": "bot@zulip.example.com",
                "api_key": "old-key",
                "channel": "alerts",
                "topic": "GlitchTip Alerts",
            },
        )
        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        data = {
            "timespanMinutes": 60,
            "quantity": 2,
            "alertRecipients": [
                {
                    "recipientType": "zulip",
                    "url": "https://zulip.example.com",
                    "botEmail": "bot@zulip.example.com",
                    "apiKey": "new-rotated-key",
                    "channel": "alerts",
                    "topic": "GlitchTip Alerts",
                }
            ],
        }
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        alert.refresh_from_db()
        self.assertEqual(alert.alertrecipient_set.count(), 1)
        recipient = alert.alertrecipient_set.first()
        self.assertEqual(recipient.config["api_key"], "new-rotated-key")

    @mock.patch("requests.post")
    def test_test_project_alert_zulip(self, mock_post):
        """Test endpoint works with Zulip recipient, passing config."""
        alert = baker.make(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.ZULIP,
            url="https://zulip.example.com/",
            config={
                "bot_email": "bot@zulip.example.com",
                "api_key": "test-key",
                "channel": "alerts",
                "topic": "GlitchTip Alerts",
            },
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = self.client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["recipientType"], "zulip")
        self.assertEqual(data[0]["status"], "sent")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["auth"], ("bot@zulip.example.com", "test-key"))
