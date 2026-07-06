from urllib.parse import unquote

from allauth.mfa.models import Authenticator
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import UserProjectAlert
from glitchtip.test_utils.test_case import GlitchTestCase

from ..models import User


class UserRegistrationTestCase(TestCase):
    def test_create_user(self):
        url = "/_allauth/browser/v1/auth/signup"
        data = {
            "email": "test@example.com",
            "password": "hunter222",
        }
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    def test_closed_registration(self):
        """Only first user may register"""
        url = "/_allauth/browser/v1/auth/signup"
        user1_data = {
            "email": "test1@example.com",
            "password": "hunter222",
        }
        user2_data = {
            "email": "test2@example.com",
            "password": "hunter222",
        }
        with override_settings(ENABLE_USER_REGISTRATION=False):
            res = self.client.post(url, user1_data, content_type="application/json")
            self.assertEqual(res.status_code, 200)

            res = self.client.post(url, user2_data, content_type="application/json")
            self.assertEqual(res.status_code, 409)

    def test_social_apps_only_registration(self):
        """Only first user may register"""
        url = "/_allauth/browser/v1/auth/signup"
        user1_data = {
            "email": "test1@example.com",
            "password": "hunter222",
        }
        user2_data = {
            "email": "test2@example.com",
            "password": "hunter222",
        }
        with override_settings(
            ENABLE_USER_REGISTRATION=False, ENABLE_SOCIAL_APPS_USER_REGISTRATION=True
        ):
            res = self.client.post(url, user1_data, content_type="application/json")
            self.assertEqual(res.status_code, 200)

            res = self.client.post(url, user2_data, content_type="application/json")
            self.assertEqual(res.status_code, 409)


class UsersTestCase(GlitchTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.create_user()

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_list(self):
        url = reverse("api:list_users")
        res = await self.async_client.get(url)
        self.assertContains(res, self.user.email)

    async def test_retrieve(self):
        url = reverse("api:get_user", args=["me"])
        res = await self.async_client.get(url)
        self.assertContains(res, self.user.email)
        url = reverse("api:get_user", args=[self.user.id])
        res = await self.async_client.get(url)
        self.assertContains(res, self.user.email)

    async def test_destroy(self):
        other_user = await baker.amake("users.user")
        url = reverse("api:delete_user", args=[other_user.pk])
        res = await self.async_client.delete(url)
        self.assertEqual(
            res.status_code, 404, "User should not be able to delete other users"
        )

        url = reverse("api:delete_user", args=[self.user.pk])
        res = await self.async_client.delete(url)
        self.assertEqual(
            res.status_code, 400, "Not allowed to destroy owned organization"
        )

        # Delete organization to allow user deletion
        await self.organization.adelete()
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(await User.objects.filter(pk=self.user.pk).aexists())

    async def test_update(self):
        url = reverse("api:update_user", args=["me"])
        data = {"name": "new", "options": {"language": "en"}}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, data["name"])
        self.assertContains(res, data["options"]["language"])
        self.assertTrue(await User.objects.filter(name=data["name"]).aexists())

    async def test_organization_members_list(self):
        other_user = await baker.amake("users.user")
        other_organization = await baker.amake("organizations_ext.Organization")
        await other_organization.aadd_user(other_user, OrganizationUserRole.ADMIN)

        user2 = await baker.amake("users.User")
        await self.organization.aadd_user(user2, OrganizationUserRole.MEMBER)
        url = reverse("api:list_organization_members", args=[self.organization.slug])
        res = await self.async_client.get(url)
        self.assertContains(res, user2.email)
        self.assertNotContains(res, other_user.email)

        # Can't view members of groups you don't belong to
        url = reverse("api:list_organization_members", args=[other_organization.slug])
        res = await self.async_client.get(url)
        self.assertNotContains(res, other_user.email)

    async def test_emails_list(self):
        email_address = await baker.amake("account.EmailAddress", user=self.user)
        another_user = await baker.amake("users.user")
        another_email_address = await baker.amake(
            "account.EmailAddress", user=another_user
        )
        url = reverse("api:list_emails", args=["me"])
        res = await self.async_client.get(url)
        self.assertContains(res, email_address.email)
        self.assertNotContains(res, another_email_address.email)

    async def test_emails_create(self):
        url = reverse("api:list_emails", args=["me"])

        res = await self.async_client.post(
            url, {"email": "invalid"}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 422)

        new_email = "new@exmaple.com"
        data = {"email": new_email}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, new_email, status_code=201)
        self.assertTrue(
            await self.user.emailaddress_set.filter(
                email=new_email, verified=False
            ).aexists()
        )
        self.assertEqual(len(mail.outbox), 1)

        # Ensure token is valid and can verify email
        body = mail.outbox[0].body
        key = unquote(body[body.find("confirm-email") :].split("/")[1])
        url = "/_allauth/browser/v1/auth/email/verify"
        data = {"key": key}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertTrue(
            await self.user.emailaddress_set.filter(
                email=new_email, verified=True
            ).aexists()
        )

    async def test_emails_create_dupe_email(self):
        url = reverse("api:create_email", args=["me"])
        email_address = await baker.amake(
            "account.EmailAddress",
            user=self.user,
            email="something@example.com",
        )
        data = {"email": email_address.email}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, "already exists", status_code=400)

    async def test_emails_create_dupe_email_other_user(self):
        url = reverse("api:create_email", args=["me"])
        email_address = await baker.amake(
            "account.EmailAddress", email="a@example.com", verified=True
        )
        data = {"email": email_address.email}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertContains(res, "already exists", status_code=400)

    async def test_emails_set_primary(self):
        url = reverse("api:set_email_as_primary", args=["me"])
        email_address = await baker.amake(
            "account.EmailAddress", verified=True, user=self.user
        )
        data = {"email": email_address.email}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertContains(res, email_address.email, status_code=200)
        self.assertTrue(
            await self.user.emailaddress_set.filter(
                email=email_address.email, primary=True
            ).aexists()
        )

        extra_email = await baker.amake(
            "account.EmailAddress", verified=True, user=self.user
        )
        data = {"email": extra_email.email}
        res = await self.async_client.put(url, data)
        self.assertEqual(
            await self.user.emailaddress_set.filter(primary=True).acount(), 1
        )

    async def test_emails_set_primary_unverified_primary(self):
        """
        Because confirmation is optional, it's possible to have an existing email that is primary and unverified
        """
        url = reverse("api:set_email_as_primary", args=["me"])
        email = "test@example.com"
        await baker.amake(
            "account.EmailAddress",
            primary=True,
            user=self.user,
        )
        await baker.amake(
            "account.EmailAddress",
            email=email,
            verified=True,
            user=self.user,
        )
        data = {"email": email}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    async def test_emails_destroy(self):
        url = reverse("api:delete_email", args=["me"])
        email_address = await baker.amake(
            "account.EmailAddress", verified=True, primary=False, user=self.user
        )
        data = {"email": email_address.email}
        res = await self.async_client.delete(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            await self.user.emailaddress_set.filter(email=email_address.email).aexists()
        )

    async def test_emails_confirm(self):
        email_address = await baker.amake("account.EmailAddress", user=self.user)
        url = reverse("api:send_confirm_email", args=["me"])
        data = {"email": email_address.email}
        res = await self.async_client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertEqual(email.extra_headers["X-Mailer"], "GlitchTip")

    async def test_notifications_retrieve(self):
        url = reverse("api:get_notifications", args=["me"])
        res = await self.async_client.get(url)
        self.assertContains(res, "subscribeByDefault")

    async def test_notifications_update(self):
        url = reverse("api:update_notifications", args=["me"])
        data = {"subscribeByDefault": False}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertFalse(res.json().get("subscribeByDefault"))
        await self.user.arefresh_from_db()
        self.assertFalse(self.user.subscribe_by_default)

    async def test_alerts_retrieve(self):
        url = reverse("api:user_notification_alerts", args=["me"])
        alert = await baker.amake(
            "projects.UserProjectAlert", user=self.user, project=self.project
        )
        res = await self.async_client.get(url)
        self.assertContains(res, self.project.id)
        self.assertEqual(res.json()[str(self.project.id)], alert.status)

    async def test_alerts_update(self):
        url = reverse("api:update_user_notification_alerts", args=["me"])

        # Set to alert to On
        data = {str(self.project.id): 1}
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(await UserProjectAlert.objects.acount(), 1)
        self.assertEqual((await UserProjectAlert.objects.afirst()).status, 1)

        # Set to alert to Off
        data = '{"' + str(self.project.id) + '":0}'
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual((await UserProjectAlert.objects.afirst()).status, 0)

        # Set to alert to "default"
        data = '{"' + str(self.project.id) + '":-1}'
        res = await self.async_client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        # Default deletes the row
        self.assertEqual(await UserProjectAlert.objects.acount(), 0)

    def test_alert_notification_recipients_default_false(self):
        User.inspect = True
        self.user.subscribe_by_default = False
        self.user.save()

        no_mail_project = baker.make("projects.Project", organization=self.organization)
        yes_mail_project = baker.make(
            "projects.Project", organization=self.organization
        )

        no_mail_project.teams.add(self.team)
        yes_mail_project.teams.add(self.team)

        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=no_mail_project,
            status=0,
        )
        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=yes_mail_project,
            status=1,
        )

        generic_alert = baker.make("alerts.ProjectAlert", project=self.project)
        no_mail_alert = baker.make("alerts.ProjectAlert", project=no_mail_project)
        yes_mail_alert = baker.make("alerts.ProjectAlert", project=yes_mail_project)

        generic_notification = baker.make(
            "alerts.Notification", project_alert=generic_alert
        )
        no_notification = baker.make("alerts.Notification", project_alert=no_mail_alert)
        yes_notification = baker.make(
            "alerts.Notification", project_alert=yes_mail_alert
        )

        self.assertEqual(
            0, User.objects.alert_notification_recipients(generic_notification).count()
        )
        self.assertEqual(
            0, User.objects.alert_notification_recipients(no_notification).count()
        )
        self.assertEqual(
            1, User.objects.alert_notification_recipients(yes_notification).count()
        )

    def test_alert_notification_recipients_default_true(self):
        self.user.subscribe_by_default = True
        self.user.save()

        no_mail_project = baker.make("projects.Project", organization=self.organization)
        yes_mail_project = baker.make(
            "projects.Project", organization=self.organization
        )

        no_mail_project.teams.add(self.team)
        yes_mail_project.teams.add(self.team)

        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=no_mail_project,
            status=0,
        )
        baker.make(
            "projects.UserProjectAlert",
            user=self.user,
            project=yes_mail_project,
            status=1,
        )

        generic_alert = baker.make("alerts.ProjectAlert", project=self.project)
        no_mail_alert = baker.make("alerts.ProjectAlert", project=no_mail_project)
        yes_mail_alert = baker.make("alerts.ProjectAlert", project=yes_mail_project)

        generic_notification = baker.make(
            "alerts.Notification", project_alert=generic_alert
        )
        no_notification = baker.make("alerts.Notification", project_alert=no_mail_alert)
        yes_notification = baker.make(
            "alerts.Notification", project_alert=yes_mail_alert
        )

        self.assertEqual(
            1, User.objects.alert_notification_recipients(generic_notification).count()
        )
        self.assertEqual(
            0, User.objects.alert_notification_recipients(no_notification).count()
        )
        self.assertEqual(
            1, User.objects.alert_notification_recipients(yes_notification).count()
        )

    async def test_reset_password(self):
        """
        Social accounts weren't getting reset password emails. This
        approximates the issue by testing an account that has an
        unusable password.
        """
        url = "/_allauth/browser/v1/auth/password/request"

        # Normal behavior
        await self.async_client.post(
            url, {"email": self.user.email}, content_type="application/json"
        )
        self.assertEqual(len(mail.outbox), 1)

        user_without_password = await baker.amake("users.User")
        user_without_password.set_unusable_password()
        await user_without_password.asave()
        self.assertFalse(user_without_password.has_usable_password())
        await self.async_client.post(
            url, {"email": user_without_password.email}, content_type="application/json"
        )
        self.assertEqual(len(mail.outbox), 2)

    async def test_generate_recovery_codes(self):
        url = reverse("api:generate_recovery_codes")
        res = await self.async_client.get(url)
        self.assertContains(res, "codes")
        code = res.json()["codes"][0]
        res = await self.async_client.post(
            url, {"code": "0"}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)
        res = await self.async_client.post(
            url,
            {"code": code},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 204)
        self.assertTrue(await Authenticator.objects.filter(user=self.user).aexists())
