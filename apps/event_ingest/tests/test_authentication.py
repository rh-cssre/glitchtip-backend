from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from model_bakery import baker
from ninja.errors import AuthenticationError

from apps.event_ingest.authentication import auth_from_request


class AuthFromRequestTestCase(TestCase):
    def test_missing_auth_raises_with_integer_status_code(self):
        """AuthenticationError must use an integer status_code, not the message string."""
        request = RequestFactory().post("/api/1/store/")
        with self.assertRaises(AuthenticationError) as ctx:
            auth_from_request(request)
        self.assertIsInstance(ctx.exception.status_code, int)


class AuthenticationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.User")
        cls.project = baker.make("projects.Project")
        cls.project_key = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization
        # Add user to organization to create OrganizationOwner (billing contact)
        cls.organization.add_user(cls.user)

    def setUp(self):
        self.url = (
            reverse("event_envelope", args=[self.project.id])
            + f"?sentry_key={self.project_key.public_key}"
        )

    def test_org_throttle(self):
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.organization.event_throttle_rate = 100
        self.organization.save()
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.headers.get("Retry-After"), "600")
        self.assertEqual(res.status_code, 429)

    def test_invalid_project_id(self):
        with self.assertRaises(NoReverseMatch):
            reverse("event_envelope", args=[f"{self.project.id}''"])
