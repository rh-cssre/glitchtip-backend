from django.shortcuts import reverse
from rest_framework.test import APITestCase


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
