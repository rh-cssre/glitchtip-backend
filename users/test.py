from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from organizations_ext.models import OrganizationUserRole
from .models import UserProjectAlerts


class OrganizationsAPITestCase(APITestCase):
    def test_organizations_retrieve(self):
        url = reverse("rest_register")
        data = {
            "email": "test@example.com",
            "password1": "hunter222",
            "password2": "hunter222",
        }
        res = self.client.post(url, data)
        self.assertContains(res, "key", status_code=201)


class UserDetailsTestCase(APITestCase):
    def test_user_details(self):
        """ User details should have email and associated social account providers """
        user = baker.make("users.user")
        socialaccount = baker.make("socialaccount.SocialAccount", user=user)
        self.client.force_login(user)
        url = reverse("rest_user_details")

        res = self.client.get(url)
        self.assertContains(res, user.email)
        self.assertContains(res, socialaccount.provider)


class UsersTestCase(APITestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user, OrganizationUserRole.ADMIN)
        self.team = baker.make("teams.Team", organization=self.organization)
        self.team.members.add(self.user)
        self.project = baker.make("projects.Project", organization=self.organization)
        self.project.team_set.add(self.team)
        self.client.force_login(self.user)

    def test_list(self):
        url = reverse("user-list")
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

    def test_retrieve(self):
        url = reverse("user-detail", args=["me"])
        res = self.client.get(url)
        self.assertContains(res, self.user.email)

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
            "users.UserProjectAlerts", user=self.user, project=self.project
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
        self.assertEqual(UserProjectAlerts.objects.all().count(), 1)
        self.assertEqual(UserProjectAlerts.objects.first().status, 1)

        # Set to alert to Off
        data = '{"' + str(self.project.id) + '":0}'
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(UserProjectAlerts.objects.first().status, 0)

        # Set to alert to "default"
        data = '{"' + str(self.project.id) + '":-1}'
        res = self.client.put(url, data, content_type="application/json")
        self.assertEqual(res.status_code, 204)
        # Default deletes the row
        self.assertEqual(UserProjectAlerts.objects.all().count(), 0)
