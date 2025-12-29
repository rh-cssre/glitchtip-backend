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

    def test_list(self):
        url = reverse("api:list_users")
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_retrieve(self):
        url = reverse("api:get_user", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, self.user.email)
        url = reverse("api:get_user", args=[self.user.id])
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_destroy(self):
        other_user = baker.make("users.user")
        url = reverse("api:delete_user", args=[other_user.pk])
        res = self.client.delete(url)
        self.assertEqual(
            res.status_code, 404, "User should not be able to delete other users"
        )

        url = reverse("api:delete_user", args=[self.user.pk])
        res = self.client.delete(url)
        self.assertEqual(
            res.status_code, 400, "Not allowed to destroy owned organization"
        )

        # Delete organization to allow user deletion
        self.organization.delete()
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_update(self):
        url = reverse("api:update_user", args=["me"])
        data = {"name": "new", "options": {"language": "en"}}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, data["name"])
        self.assertContains(res, data["options"]["language"])
        self.assertTrue(User.objects.filter(name=data["name"]).exists())

    def test_organization_members_list(self):
        other_user = baker.make("users.user")
        other_organization = baker.make("organizations_ext.Organization")
        other_organization.add_user(other_user, OrganizationUserRole.ADMIN)

        user2 = baker.make("users.User")
        self.organization.add_user(user2, OrganizationUserRole.MEMBER)
        url = reverse("api:list_organization_members", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, user2.email)
        self.assertNotContains(res, other_user.email)

        # Can't view members of groups you don't belong to
        url = reverse("api:list_organization_members", args=[other_organization.slug])
        res = self.client.get(url)
        self.assertNotContains(res, other_user.email)

    def test_emails_list(self):
        email_address = baker.make("account.EmailAddress", user=self.user)
        another_user = baker.make("users.user")
        another_email_address = baker.make("account.EmailAddress", user=another_user)
        url = reverse("api:list_emails", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, email_address.email)
        self.assertNotContains(res, another_email_address.email)

    def test_emails_create(self):
        url = reverse("api:list_emails", args=["me"])

        res = self.client.post(
            url, {"email": "invalid"}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 422)

        new_email = "new@exmaple.com"
        data = {"email": new_email}
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, new_email, status_code=201)
        self.assertTrue(
            self.user.emailaddress_set.filter(email=new_email, verified=False).exists()
        )
        self.assertEqual(len(mail.outbox), 1)

        # Ensure token is valid and can verify email
        body = mail.outbox[0].body
        key = unquote(body[body.find("confirm-email") :].split("/")[1])
        url = "/_allauth/browser/v1/auth/email/verify"
        data = {"key": key}
        res = self.client.post(url, data, content_type="application/json")
        self.assertTrue(
            self.user.emailaddress_set.filter(email=new_email, verified=True).exists()
        )

    def test_emails_create_dupe_email(self):
        url = reverse("api:create_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress",
            user=self.user,
            email="something@example.com",
        )
        data = {"email": email_address.email}
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, "already exists", status_code=400)

    def test_emails_create_dupe_email_other_user(self):
        url = reverse("api:create_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", email="a@example.com", verified=True
        )
        data = {"email": email_address.email}
        res = self.client.post(url, data, content_type="application/json")
        self.assertContains(res, "already exists", status_code=400)

    def test_emails_set_primary(self):
        url = reverse("api:set_email_as_primary", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.put(url, data, content_type="application/json")
        self.assertContains(res, email_address.email, status_code=200)
        self.assertTrue(
            self.user.emailaddress_set.filter(
                email=email_address.email, primary=True
            ).exists()
        )

        extra_email = baker.make("account.EmailAddress", verified=True, user=self.user)
        data = {"email": extra_email.email}
        res = self.client.put(url, data)
        self.assertEqual(self.user.emailaddress_set.filter(primary=True).count(), 1)

    def test_emails_set_primary_unverified_primary(self):
        """
        Because confirmation is optional, it's possible to have an existing email that is primary and unverified
        """
        url = reverse("api:set_email_as_primary", args=["me"])
        email = "test@example.com"
        baker.make(
            "account.EmailAddress",
            primary=True,
            user=self.user,
        )
        baker.make(
            "account.EmailAddress",
            email=email,
            verified=True,
            user=self.user,
        )
        data = {"email": email}
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    def test_emails_destroy(self):
        url = reverse("api:delete_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, primary=False, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.delete(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            self.user.emailaddress_set.filter(email=email_address.email).exists()
        )

    def test_emails_confirm(self):
        email_address = baker.make("account.EmailAddress", user=self.user)
        url = reverse("api:send_confirm_email", args=["me"])
        data = {"email": email_address.email}
        res = self.client.post(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertEqual(email.extra_headers["X-Mailer"], "GlitchTip")

    def test_notifications_retrieve(self):
        url = reverse("api:get_notifications", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, "subscribeByDefault")

    def test_notifications_update(self):
        url = reverse("api:update_notifications", args=["me"])
        data = {"subscribeByDefault": False}
        res = self.client.put(url, data, content_type="application/json")
        self.assertFalse(res.json().get("subscribeByDefault"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.subscribe_by_default)

    def test_alerts_retrieve(self):
        url = reverse("api:user_notification_alerts", args=["me"])
        alert = baker.make(
            "projects.UserProjectAlert", user=self.user, project=self.project
        )
        res = self.client.get(url)
        self.assertContains(res, self.project.id)
        self.assertEqual(res.json()[str(self.project.id)], alert.status)

    def test_alerts_update(self):
        url = reverse("api:update_user_notification_alerts", args=["me"])

        # Set to alert to On
        data = {str(self.project.id): 1}
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(UserProjectAlert.objects.all().count(), 1)
        self.assertEqual(UserProjectAlert.objects.first().status, 1)

        # Set to alert to Off
        data = '{"' + str(self.project.id) + '":0}'
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(UserProjectAlert.objects.first().status, 0)

        # Set to alert to "default"
        data = '{"' + str(self.project.id) + '":-1}'
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        # Default deletes the row
        self.assertEqual(UserProjectAlert.objects.all().count(), 0)

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

    def test_reset_password(self):
        """
        Social accounts weren't getting reset password emails. This
        approximates the issue by testing an account that has an
        unusable password.
        """
        url = "/_allauth/browser/v1/auth/password/request"

        # Normal behavior
        self.client.post(
            url, {"email": self.user.email}, content_type="application/json"
        )
        self.assertEqual(len(mail.outbox), 1)

        user_without_password = baker.make("users.User")
        user_without_password.set_unusable_password()
        user_without_password.save()
        self.assertFalse(user_without_password.has_usable_password())
        self.client.post(
            url, {"email": user_without_password.email}, content_type="application/json"
        )
        self.assertEqual(len(mail.outbox), 2)

    def test_generate_recovery_codes(self):
        url = reverse("api:generate_recovery_codes")
        res = self.client.get(url)
        self.assertContains(res, "codes")
        code = res.json()["codes"][0]
        res = self.client.post(url, {"code": "0"}, content_type="application/json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            url,
            {"code": code},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 204)
        self.assertTrue(Authenticator.objects.filter(user=self.user).exists())
