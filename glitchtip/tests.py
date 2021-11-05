from django.shortcuts import reverse
from django.test import TestCase


class DocsTestCase(TestCase):
    def test_redoc(self):
        url = reverse("schema-redoc")
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

    def test_openapi(self):
        url = reverse("schema-redoc") + "?format=openapi"
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
