from django.test import TestCase
from django.urls import reverse_lazy


class SettingsTestCase(TestCase):
    def test_settings(self):
        with self.assertNumQueries(1):
            res = self.client.get(reverse_lazy("api:get_settings"))
        self.assertEqual(res.status_code, 200)
