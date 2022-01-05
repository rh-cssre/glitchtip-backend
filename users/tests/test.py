from django.core import mail
from django.test import override_settings
from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from glitchtip.test_utils.test_case import GlitchTipTestCase
from organizations_ext.models import OrganizationUserRole
from ..models import UserProjectAlert, User


class UserRegistrationTestCase(APITestCase):
    def test_create_user(self):
        url = reverse("rest_register")
        data = {
            "email": "test@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 204)

    def test_create_user_with_tags(self):
        url = reverse("rest_register")
        data = {
            "email": "test@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
            "tags": "?utm_campaign=test&utm_source=test&utm_medium=test&utm_medium=test",
        }
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 204)
        self.assertTrue(
            User.objects.filter(analytics__register__utm_campaign="test").exists()
        )

    def test_closed_registration(self):
        """ Only first user/organization may register """
        url = reverse("rest_register")
        org_url = reverse("organization-list")
        data = {
            "email": "test@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
        }
        org_data = {"name": "test"}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 204)
        baker.make("organizations_ext.Organization")
        data["email"] = "another@example.com"
        with override_settings(ENABLE_OPEN_USER_REGISTRATION=False):
            res = self.client.post(url, data)
            self.assertEqual(res.status_code, 204)
            # Can't make more organizations outside of Django Admin
            user = User.objects.first()
            self.client.force_login(user)
            res = self.client.post(org_url, org_data)
            self.assertEqual(res.status_code, 403)
        # When True, users can register and create more orgs
        with override_settings(ENABLE_OPEN_USER_REGISTRATION=True):
            # Can't make more organizations outside of Django Admin
            user = User.objects.first()
            self.client.force_login(user)
            res = self.client.post(org_url, org_data)
            self.assertEqual(res.status_code, 201)


class UsersTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_list(self):
        url = reverse("user-list")
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_retrieve(self):
        url = reverse("user-detail", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_destroy(self):
        other_user = baker.make("users.user")
        url = reverse("user-detail", args=[other_user.pk])
        res = self.client.delete(url)
        self.assertEqual(
            res.status_code, 404, "User should not be able to delete other users"
        )

        url = reverse("user-detail", args=[self.user.pk])
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_organization_members_list(self):
        other_user = baker.make("users.user")
        other_organization = baker.make("organizations_ext.Organization")
        other_organization.add_user(other_user, OrganizationUserRole.ADMIN)

        user2 = baker.make("users.User")
        self.organization.add_user(user2, OrganizationUserRole.MEMBER)
        url = reverse("organization-members-list", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, user2.email)
        self.assertNotContains(res, other_user.email)

        # Can't view members of groups you don't belong to
        url = reverse("organization-members-list", args=[other_organization.slug])
        res = self.client.get(url)
        self.assertNotContains(res, other_user.email)

    def test_emails_retrieve(self):
        email_address = baker.make("account.EmailAddress", user=self.user)
        another_user = baker.make("users.user")
        another_email_address = baker.make("account.EmailAddress", user=another_user)
        url = reverse("user-emails-list", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, email_address.email)
        self.assertNotContains(res, another_email_address.email)

    def test_emails_confirm(self):
        email_address = baker.make("account.EmailAddress", user=self.user)
        url = reverse("user-emails-list", args=["me"]) + "confirm/"
        data = {"email": email_address.email}
        res = self.client.post(url, data)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)

    def test_emails_create(self):
        url = reverse("user-emails-list", args=["me"])
        new_email = "new@exmaple.com"
        data = {"email": new_email}
        res = self.client.post(url, data)
        self.assertContains(res, new_email, status_code=201)
        self.assertTrue(
            self.user.emailaddress_set.filter(email=new_email, verified=False).exists()
        )
        self.assertEqual(len(mail.outbox), 1)

        # Ensure token is valid and can verify email
        body = mail.outbox[0].body
        key = body[body.find("confirm-email") :].split("/")[1]
        url = reverse("rest_verify_email")
        data = {"key": key}
        res = self.client.post(url, data)
        self.assertTrue(
            self.user.emailaddress_set.filter(email=new_email, verified=True).exists()
        )

    def test_emails_create_dupe_email(self):
        url = reverse("user-emails-list", args=["me"])
        email_address = baker.make("account.EmailAddress", user=self.user)
        data = {"email": email_address.email}
        res = self.client.post(url, data)
        self.assertContains(res, "this account", status_code=400)

    def test_emails_create_dupe_email_other_user(self):
        url = reverse("user-emails-list", args=["me"])
        email_address = baker.make("account.EmailAddress")
        data = {"email": email_address.email}
        res = self.client.post(url, data)
        self.assertContains(res, "another account", status_code=400)

    def test_emails_set_primary(self):
        url = reverse("user-emails-list", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.put(url, data)
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

    def test_emails_destroy(self):
        url = reverse("user-emails-list", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, primary=False, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.delete(url, data)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            self.user.emailaddress_set.filter(email=email_address.email).exists()
        )

    def test_notifications_retrieve(self):
        url = reverse("user-detail", args=["me"]) + "notifications/"
        res = self.client.get(url)
        self.assertContains(res, "subscribeByDefault")

    def test_notifications_update(self):
        url = reverse("user-detail", args=["me"]) + "notifications/"
        data = {"subscribeByDefault": False}
        res = self.client.put(url, data)
        self.assertFalse(res.data.get("subscribeByDefault"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.subscribe_by_default)

    def test_alerts_retrieve(self):
        url = reverse("user-detail", args=["me"]) + "notifications/alerts/"
        alert = baker.make(
            "users.UserProjectAlert", user=self.user, project=self.project
        )
        res = self.client.get(url)
        self.assertContains(res, self.project.id)
        self.assertEqual(res.data[self.project.id], alert.status)

    def test_alerts_update(self):
        url = reverse("user-detail", args=["me"]) + "notifications/alerts/"

        # Set to alert to On
        data = '{"' + str(self.project.id) + '":1}'
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

    def test_reset_password(self):
        url = reverse("rest_password_reset")

        # Normal behavior
        res = self.client.post(url, {"email": self.user.email})
        self.assertEqual(len(mail.outbox), 1)

        """
        Social accounts weren't getting reset password emails. This
        approximates the issue by testing an account that has an
        unusable password.
        """
        user_without_password = baker.make("users.User")
        user_without_password.set_unusable_password()
        user_without_password.save()
        self.assertFalse(user_without_password.has_usable_password())
        res = self.client.post(url, {"email": user_without_password.email})
        self.assertEqual(len(mail.outbox), 2)
