from django.shortcuts import reverse
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient, APITestCase


class ObservabilityAPITestCase(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def test_get_metrics(self):
        self.user = baker.make("users.user", is_staff=True)
        self.client.force_login(self.user)
        url = reverse("prometheus-django-metrics")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
