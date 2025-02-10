from django.test import TestCase
from django.urls import reverse
from model_bakery import baker


class AuthenticationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = baker.make("projects.Project")
        cls.project_key = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization

    def setUp(self):
        self.url = reverse(
            "api:event_envelope", args=[self.project.id]
        ) + f"?sentry_key={self.project_key.public_key}"

    def test_org_throttle(self):
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.organization.event_throttle_rate = 100
        self.organization.save()
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.headers.get('Retry-After'), '600')
        self.assertEqual(res.status_code, 429)
