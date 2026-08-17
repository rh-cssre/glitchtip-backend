from django.test import RequestFactory, TestCase, TransactionTestCase
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


class AuthenticationTestCase(TransactionTestCase):
    def setUp(self):
        # Async ingest path reads project auth via async_connections, which
        # only sees committed data — TransactionTestCase commits per-test
        # fixtures, then truncates between tests.
        self.user = baker.make("users.User")
        self.project = baker.make("projects.Project")
        self.project_key = self.project.projectkey_set.first()
        self.organization = self.project.organization
        # Add user to organization to create OrganizationOwner (billing contact)
        self.organization.add_user(self.user)
        self.url = (
            reverse("event_envelope", args=[self.project.id])
            + f"?sentry_key={self.project_key.public_key}"
        )

    async def test_org_throttle(self):
        res = await self.async_client.post(
            self.url, [{}], content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        self.organization.event_throttle_rate = 100
        await self.organization.asave()
        res = await self.async_client.post(
            self.url, [{}], content_type="application/json"
        )
        self.assertEqual(res.headers.get("Retry-After"), "600")
        self.assertEqual(res.status_code, 429)

    def test_invalid_project_id(self):
        with self.assertRaises(NoReverseMatch):
            reverse("event_envelope", args=[f"{self.project.id}''"])

    async def test_invalid_dsn_does_not_block_valid_dsn(self):
        """A request with a bad sentry_key must not block a valid DSN on the
        same project. Protects against a trivial DoS where any public
        project_id + random key locks out the project's real traffic.
        """
        bad_key = "00000000000000000000000000000000"
        bad_url = (
            reverse("event_envelope", args=[self.project.id]) + f"?sentry_key={bad_key}"
        )
        res = await self.async_client.post(
            bad_url, [{}], content_type="application/json"
        )
        self.assertEqual(res.status_code, 403)

        # Valid DSN on the same project must still be accepted while the
        # invalid-DSN block is in its TTL window.
        res = await self.async_client.post(
            self.url, [{}], content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

        # The bad key stays blocked (served from cache, no DB hit).
        res = await self.async_client.post(
            bad_url, [{}], content_type="application/json"
        )
        self.assertEqual(res.status_code, 403)
