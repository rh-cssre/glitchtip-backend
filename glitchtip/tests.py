from django.test import TestCase
from django.urls import reverse


class SettingsTestCase(TestCase):
    def test_settings(self):
        url = reverse("api:get_settings")
        res = self.client.get(url)  # Check that no auth is necessary
        self.assertEqual(res.status_code, 200)
        with self.assertNumQueries(1):
            self.client.get(url)
