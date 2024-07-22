from django.core import mail
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
from rest_framework.test import APITestCase

from apps.organizations_ext.models import OrganizationUserRole
from apps.projects.models import UserProjectAlert
from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import User


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
        """Only first user may register"""
        url = reverse("rest_register")
        user1_data = {
            "email": "test1@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
        }
        user2_data = {
            "email": "test2@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
        }
        with override_settings(ENABLE_USER_REGISTRATION=False):
            res = self.client.post(url, user1_data)
            self.assertEqual(res.status_code, 204)

            res = self.client.post(url, user2_data)
            self.assertEqual(res.status_code, 403)


class UsersTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_logged_in_user()

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
        data = {"name": "new", "options": {"foo": "bar"}}
        res = self.client.put(url, data, format="json")
        self.assertContains(res, data["name"])
        self.assertContains(res, data["options"]["foo"])
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

        res = self.client.post(url, {"email": "invalid"}, format="json")
        self.assertEqual(res.status_code, 422)

        new_email = "new@exmaple.com"
        data = {"email": new_email}
        res = self.client.post(url, data, format="json")
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
        url = reverse("api:create_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress",
            user=self.user,
            email="something@example.com",
        )
        data = {"email": email_address.email}
        res = self.client.post(url, data, format="json")
        self.assertContains(res, "already exists", status_code=400)

    def test_emails_create_dupe_email_other_user(self):
        url = reverse("api:create_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", email="a@example.com", verified=True
        )
        data = {"email": email_address.email}
        res = self.client.post(url, data, format="json")
        self.assertContains(res, "already exists", status_code=400)

    def test_emails_set_primary(self):
        url = reverse("api:set_email_as_primary", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.put(url, data, format="json")
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
        res = self.client.put(url, data, format="json")
        self.assertEqual(res.status_code, 200)

    def test_emails_destroy(self):
        url = reverse("api:delete_email", args=["me"])
        email_address = baker.make(
            "account.EmailAddress", verified=True, primary=False, user=self.user
        )
        data = {"email": email_address.email}
        res = self.client.delete(url, data, format="json")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            self.user.emailaddress_set.filter(email=email_address.email).exists()
        )

    def test_emails_confirm(self):
        email_address = baker.make("account.EmailAddress", user=self.user)
        url = reverse("api:send_confirm_email", args=["me"])
        data = {"email": email_address.email}
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(len(mail.outbox), 1)

    def test_notifications_retrieve(self):
        url = reverse("api:get_notifications", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, "subscribeByDefault")

    def test_notifications_update(self):
        url = reverse("api:update_notifications", args=["me"])
        data = {"subscribeByDefault": False}
        res = self.client.put(url, data, format="json")
        self.assertFalse(res.json().get("subscribeByDefault"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.subscribe_by_default)

    def test_alerts_retrieve(self):
        url = reverse("user-detail", args=["me"]) + "notifications/alerts/"
        alert = baker.make(
            "projects.UserProjectAlert", user=self.user, project=self.project
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
        """
        Social accounts weren't getting reset password emails. This
        approximates the issue by testing an account that has an
        unusable password.
        """
        url = reverse("rest_password_reset")

        # Normal behavior
        self.client.post(url, {"email": self.user.email})
        self.assertEqual(len(mail.outbox), 1)

        user_without_password = baker.make("users.User")
        user_without_password.set_unusable_password()
        user_without_password.save()
        self.assertFalse(user_without_password.has_usable_password())
        self.client.post(url, {"email": user_without_password.email})
        self.assertEqual(len(mail.outbox), 2)
