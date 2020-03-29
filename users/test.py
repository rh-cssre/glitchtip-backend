from django.shortcuts import reverse
from rest_framework.test import APITestCase
from model_bakery import baker


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
