from django.urls import reverse_lazy
from django.test import TestCase


class SettingsTestCase(TestCase):
    def test_settings(self):
        with self.assertNumQueries(1):
            res = self.client.get(reverse_lazy("api-1.0.0:get_settings"))
        self.assertEqual(res.status_code, 200)
