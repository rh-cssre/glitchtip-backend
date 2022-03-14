from django.shortcuts import reverse

from rest_framework.test import APIClient
from rest_framework.test import APITestCase
from rest_framework import status
from model_bakery import baker


class ObservabilityAPITestCase(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def test_get_metrics(self):
        self.user = baker.make("users.user", is_staff=True)
        self.client.force_login(self.user)
        url = reverse("prometheus-django-metrics")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

