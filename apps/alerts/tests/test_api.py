from unittest import mock

from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import Project
from glitchtip.test_utils.aiohttp_mock import basic_auth_credentials
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import MAX_ALERT_ENVIRONMENTS, RecipientType
from ..models import ProjectAlert


class AlertAPITestCase(GlitchTipTestCaseMixin, TestCase):
    def setUp(self):
        self.create_logged_in_user()
        self.async_client.force_login(self.user)

    async def test_project_alerts_list(self):
        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )

        # Should not show up
        await baker.amake("alerts.ProjectAlert", timespan_minutes=60)
        # Second team could cause duplicates
        team2 = await baker.amake("teams.Team", organization=self.organization)
        await team2.members.aadd(self.org_user)
        await self.project.teams.aadd(team2)

        url = reverse(
            "api:list_project_alerts", args=[self.organization.slug, self.project.slug]
        )
        res = await self.async_client.get(url)
        self.assertContains(res, alert.id)
        self.assertEqual(len(res.json()), 1)

    async def test_project_alerts_create(self):
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
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        project_alert = await ProjectAlert.objects.filter(
            name="foo", uptime=True
        ).afirst()
        self.assertEqual(project_alert.timespan_minutes, data["timespanMinutes"])
        self.assertEqual(project_alert.project_id, self.project.id)
        # Check that all recipients were created
        self.assertEqual(await project_alert.alertrecipient_set.acount(), 6)
        created = [r async for r in project_alert.alertrecipient_set.all()]
        for i, recipient in enumerate(created):
            self.assertEqual(recipient.tags_to_add, recipients[i]["tagsToAdd"])

    async def test_project_alerts_environments(self):
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        data = {
            "name": "foo",
            "timespanMinutes": 60,
            "quantity": 2,
            # Blanks and duplicates are normalized away so they cannot silently
            # widen the filter to every environment
            "environments": ["production", " staging ", "", "production"],
        }
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["environments"], ["production", "staging"])
        alert = await ProjectAlert.objects.filter(name="foo").afirst()
        self.assertEqual(alert.environments, ["production", "staging"])

        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        # Omitting the field leaves it alone, matching how this endpoint treats
        # every other field it receives
        res = await self.async_client.put(
            url,
            {"name": "foo", "timespanMinutes": 60, "quantity": 2},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["environments"], ["production", "staging"])

        # Sending an empty list clears it, restoring alerts on all environments
        res = await self.async_client.put(
            url,
            {"name": "foo", "timespanMinutes": 60, "quantity": 2, "environments": []},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["environments"], [])

    async def test_project_alerts_environments_cap(self):
        """A caller cannot grow the array, or the IN clause built from it, without limit"""
        url = reverse(
            "api:create_project_alert", args=[self.organization.slug, self.project.slug]
        )
        data = {
            "name": "foo",
            "timespanMinutes": 60,
            "quantity": 2,
            "environments": [f"env-{i}" for i in range(MAX_ALERT_ENVIRONMENTS + 1)],
        }
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

        data["environments"] = data["environments"][:MAX_ALERT_ENVIRONMENTS]
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

    async def test_project_alerts_create_invalid_recipient_type(self):
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
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    async def test_project_alerts_update_all_types(self):
        alert = await baker.amake(
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
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        await alert.arefresh_from_db()
        self.assertEqual(await alert.alertrecipient_set.acount(), 4)
        updated = [r async for r in alert.alertrecipient_set.all()]
        for i, recipient in enumerate(updated):
            self.assertEqual(recipient.tags_to_add, recipients[i]["tagsToAdd"])

    async def test_project_alerts_create_permissions(self):
        user = await baker.amake("users.user")
        org_user = await self.organization.aadd_user(user, OrganizationUserRole.MEMBER)

        await self.async_client.aforce_login(user)
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
        res = await self.async_client.post(url, data, content_type="application/json")
        # Creating alerts needs admin or above
        self.assertEqual(res.status_code, 403)

        org_user.role = OrganizationUserRole.ADMIN
        await org_user.asave()
        # Add second team to ensure we don't get MultipleObjectsReturned
        team2 = await baker.amake("teams.Team", organization=self.organization)
        await team2.members.aadd(org_user)
        await self.project.teams.aadd(team2)

        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)

        org_user.role = OrganizationUserRole.MEMBER
        await org_user.asave()
        res = await self.async_client.get(url)
        # Members can still view alerts
        self.assertEqual(len(res.json()), 1)

    def alert_write_requests(self, alert, organization_id_or_slug=None):
        """(method, url, success status) for create, update and test"""
        args = [organization_id_or_slug or self.organization.slug, alert.project.slug]
        return [
            ("post", reverse("api:create_project_alert", args=args), 201),
            ("put", reverse("api:update_project_alert", args=[*args, alert.pk]), 200),
            ("post", reverse("api:test_project_alert", args=[*args, alert.pk]), 200),
        ]

    async def alert_write(self, method: str, url: str):
        data = {"timespanMinutes": 1, "quantity": 1}
        return await getattr(self.async_client, method)(
            url, data, content_type="application/json"
        )

    async def test_alert_writes_need_admin(self):
        """Create, update and test need admin or above. A member on one of
        the project's teams does not qualify."""
        alert = await baker.amake("alerts.ProjectAlert", project=self.project)
        user = await baker.amake("users.user")
        org_user = await self.organization.aadd_user(user, OrganizationUserRole.MEMBER)
        await self.team.members.aadd(org_user)
        await self.async_client.aforce_login(user)

        for method, url, _ in self.alert_write_requests(alert):
            with self.subTest(url=url):
                res = await self.alert_write(method, url)
                self.assertEqual(res.status_code, 403)
                self.assertIn("Member", res.json()["detail"])

        org_user.role = OrganizationUserRole.ADMIN
        await org_user.asave()
        for method, url, status in self.alert_write_requests(alert):
            with self.subTest(url=url):
                res = await self.alert_write(method, url)
                self.assertEqual(res.status_code, status)

    async def test_alert_writes_by_org_id(self):
        alert = await baker.amake("alerts.ProjectAlert", project=self.project)

        for method, url, status in self.alert_write_requests(
            alert, self.organization.id
        ):
            with self.subTest(url=url):
                res = await self.alert_write(method, url)
                self.assertEqual(res.status_code, status)

    async def test_alert_writes_non_member(self):
        other_org = await baker.amake("organizations_ext.Organization")
        other_project = await baker.amake(
            "projects.Project", organization=other_org, slug=self.project.slug
        )
        alert = await baker.amake("alerts.ProjectAlert", project=other_project)

        for org in [other_org.slug, other_org.id]:
            for method, url, _ in self.alert_write_requests(alert, org):
                with self.subTest(url=url):
                    res = await self.alert_write(method, url)
                    self.assertEqual(res.status_code, 404)

    async def test_alert_writes_other_project(self):
        """An alert is reachable only under its own project's path."""
        sibling = await baker.amake("projects.Project", organization=self.organization)
        alert = await baker.amake("alerts.ProjectAlert", project=sibling)
        args = [self.organization.slug, self.project.slug, alert.pk]

        for method, name in [("put", "update"), ("post", "test")]:
            with self.subTest(name=name):
                url = reverse(f"api:{name}_project_alert", args=args)
                res = await self.alert_write(method, url)
                self.assertEqual(res.status_code, 404)

    async def test_alert_writes_deleted_project(self):
        alert = await baker.amake("alerts.ProjectAlert", project=self.project)
        await Project.objects.filter(id=self.project.id).aupdate(is_deleted=True)

        for method, url, _ in self.alert_write_requests(alert):
            with self.subTest(url=url):
                res = await self.alert_write(method, url)
                self.assertEqual(res.status_code, 404)

    async def test_project_alerts_update(self):
        alert = await baker.amake(
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
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["alertRecipients"][0]["url"])

        # Webhooks require url
        data = {
            "alertRecipients": [
                {"recipientType": "discord", "url": ""},
            ],
        }
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    async def test_project_alerts_update_auth(self):
        """Cannot update alert on project that user does not belong to"""
        alert = await baker.amake("alerts.ProjectAlert", timespan_minutes=60)
        url = reverse(
            "api:update_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        data = {"timespanMinutes": 500, "quantity": 2}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 404)

    async def test_project_alerts_delete(self):
        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:delete_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = await self.async_client.delete(url, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await ProjectAlert.objects.acount(), 0)

    async def test_delete_with_second_team(self):
        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        url = reverse(
            "api:delete_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        team2 = await baker.amake("teams.Team", organization=self.organization)
        await team2.members.aadd(self.org_user)
        await self.project.teams.aadd(team2)
        res = await self.async_client.delete(url, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await ProjectAlert.objects.acount(), 0)

    async def test_alerts_of_a_soft_deleted_project_are_unreachable(self):
        alert = await baker.amake("alerts.ProjectAlert", project=self.project)
        await Project.objects.filter(pk=self.project.pk).aupdate(is_deleted=True)
        args = [self.organization.slug, self.project.slug]
        res = await self.async_client.get(reverse("api:list_project_alerts", args=args))
        self.assertEqual(res.json(), [])
        res = await self.async_client.delete(
            reverse("api:delete_project_alert", args=[*args, alert.pk])
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(await ProjectAlert.objects.filter(pk=alert.pk).aexists())

    @mock.patch("aiohttp.ClientSession")
    async def test_test_project_alert(self, MockSession):
        from apps.alerts.tests.test_webhooks import _mock_aiohttp_session

        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        await baker.amake(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="https://example.com/webhook",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = await self.async_client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["recipientType"], "webhook")
        self.assertEqual(data[0]["status"], "sent")
        mock_post.assert_called_once()

    @mock.patch("aiohttp.ClientSession")
    async def test_test_project_alert_skips_email(self, MockSession):
        from apps.alerts.tests.test_webhooks import _mock_aiohttp_session

        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        await baker.amake(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.EMAIL,
            url="",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = await self.async_client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "skipped")
        mock_post.assert_not_called()

    @mock.patch(
        "apps.alerts.api.send_test_notification",
        new_callable=mock.AsyncMock,
        side_effect=Exception("Connection refused"),
    )
    async def test_test_project_alert_error(self, _mock_send):
        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        await baker.amake(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.NTFY,
            url="https://ntfy.sh/test-topic",
        )
        url = reverse(
            "api:test_project_alert",
            args=[self.organization.slug, self.project.slug, alert.pk],
        )
        res = await self.async_client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "error")
        self.assertEqual(data[0]["message"], "Connection refused")

    async def test_project_alerts_create_zulip(self):
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
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        alert = await ProjectAlert.objects.aget(name="zulip-test")
        recipient = await alert.alertrecipient_set.afirst()
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

    async def test_project_alerts_update_zulip_config(self):
        """Updating a Zulip recipient's config (e.g. rotating API key) works."""
        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        await baker.amake(
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
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        await alert.arefresh_from_db()
        self.assertEqual(await alert.alertrecipient_set.acount(), 1)
        recipient = await alert.alertrecipient_set.afirst()
        self.assertEqual(recipient.config["api_key"], "new-rotated-key")

    @mock.patch("aiohttp.ClientSession")
    async def test_test_project_alert_zulip(self, MockSession):
        """Test endpoint works with Zulip recipient, passing config."""
        from apps.alerts.tests.test_webhooks import _mock_aiohttp_session

        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        alert = await baker.amake(
            "alerts.ProjectAlert", project=self.project, timespan_minutes=60
        )
        await baker.amake(
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
        res = await self.async_client.post(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["recipientType"], "zulip")
        self.assertEqual(data[0]["status"], "sent")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(
            basic_auth_credentials(call_kwargs["headers"]["Authorization"]),
            "bot@zulip.example.com:test-key",
        )
