from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from model_bakery import baker
from ninja.errors import AuthenticationError

from apps.event_ingest import authentication
from apps.event_ingest.authentication import auth_from_request
from apps.event_ingest.constants import EVENT_BLOCK_CACHE_KEY


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
        cache.clear()
        self.addCleanup(cache.clear)
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

    def store_url(self, project_id):
        return f"/api/{project_id}/store/?sentry_key={self.project_key.public_key}"

    async def post_event(self, url=None):
        return await self.async_client.post(
            url or self.url, [{}], content_type="application/json"
        )

    async def assertBlockedFromCache(self, url=None):
        """A repeat request is rejected from cache, without a project lookup.

        Also pins the serialized cache value: the Rust ingest path reads the
        same key out of the same Valkey, so the format is a contract between
        the two implementations rather than an internal detail.
        """
        self.assertEqual(
            cache.get(EVENT_BLOCK_CACHE_KEY + str(self.project.id)), "t:100:100"
        )
        real = authentication.get_project_auth_info_row
        with mock.patch.object(
            authentication, "get_project_auth_info_row", wraps=real
        ) as lookup:
            res = await self.post_event(url)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(
            res.headers.get("Retry-After"),
            str(authentication.HARD_THROTTLE_RETRY_AFTER),
        )
        lookup.assert_not_called()

    async def test_org_throttle(self):
        res = await self.post_event()
        self.assertEqual(res.status_code, 200)
        self.organization.event_throttle_rate = 100
        await self.organization.asave()
        res = await self.post_event()
        self.assertEqual(res.headers.get("Retry-After"), "600")
        self.assertEqual(res.status_code, 429)
        # A 100% throttle is absolute, so the block cache must absorb the
        # follow-up flood rather than repeating the lookup for every event.
        await self.assertBlockedFromCache()

    async def test_org_not_accepting_events(self):
        """is_accepting_events=False is the manual, durable org kill switch.

        Unlike event_throttle_rate it is never rewritten by the quota check,
        so it is what an operator sets to stop an org's traffic outright.
        """
        self.organization.is_accepting_events = False
        await self.organization.asave()
        # The store endpoint, unlike the envelope one, is served by this auth
        # path in a deployment with the Rust pipeline in front.
        store_url = self.store_url(self.project.id)
        res = await self.post_event(store_url)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.headers.get("Retry-After"), "600")
        await self.assertBlockedFromCache(store_url)

    async def test_block_cache_key_ignores_project_id_spelling(self):
        """A project id's spelling must not open a way around the block.

        The store and security routes capture the id as text, so "7", "007"
        and " 7 " arrive as written while naming one project. Keyed on the
        raw text they would be separate cache entries, and a flood could walk
        past a block indefinitely, paying a project lookup on each new
        spelling — the exact cost the block cache exists to avoid.
        """
        self.organization.is_accepting_events = False
        await self.organization.asave()
        pid = self.project.id
        res = await self.post_event(self.store_url(pid))
        self.assertEqual(res.status_code, 429)

        real = authentication.get_project_auth_info_row
        with mock.patch.object(
            authentication, "get_project_auth_info_row", wraps=real
        ) as lookup:
            for spelling in [f"00{pid}", f"000{pid}", f" {pid} ", f"+{pid}"]:
                res = await self.post_event(self.store_url(spelling))
                self.assertEqual(res.status_code, 429, spelling)
        lookup.assert_not_called()

    async def test_unparseable_project_id_is_rejected(self):
        """A non-numeric id is a bad request, not an unhandled 500."""
        for spelling in ["abc", "1" * 4400, "-1"]:
            res = await self.post_event(self.store_url(spelling))
            self.assertLess(res.status_code, 500, spelling)

    async def test_project_throttle(self):
        """A 100% project throttle blocks without the organization being over."""
        self.project.event_throttle_rate = 100
        await self.project.asave()
        res = await self.post_event()
        self.assertEqual(res.status_code, 429)
        await self.assertBlockedFromCache()

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
