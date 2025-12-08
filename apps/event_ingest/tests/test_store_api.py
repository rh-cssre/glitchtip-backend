from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from apps.issue_events.constants import IssueEventType
from apps.issue_events.models import IssueEvent

from .utils import EventIngestTestCase


class StoreAPITestCase(EventIngestTestCase):
    """
    These test specifically test the store API and act more of integration test
    Use test_process_issue_events.py for testing Event Ingest more specifically
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("api:event_store", args=[self.project.id]) + self.params
        self.event = self.get_json_data("events/test_data/py_hi_event.json")

    def tearDown(self):
        cache.clear()

    def test_store_api(self):
        with self.assertNumQueries(18):
            res = self.client.post(
                self.url, self.event, content_type="application/json"
            )
        self.assertContains(res, self.event["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    @override_settings(MAINTENANCE_EVENT_FREEZE=True)
    def test_maintenance_freeze(self):
        res = self.client.post(self.url, self.event, content_type="application/json")
        self.assertEqual(res.status_code, 503)

    def test_store_duplicate(self):
        """Unlike OSS Sentry, we just accept the duplicate as a performance optimization"""
        for _ in range(2):
            self.client.post(self.url, self.event, content_type="application/json")
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_store_invalid_key(self):
        params = "?sentry_key=lol"
        url = reverse("api:event_store", args=[self.project.id]) + params
        res = self.client.post(url, self.event, content_type="application/json")
        self.assertEqual(res.status_code, 422)

    def test_store_api_auth_failure(self):
        params = "?sentry_key=8bea9cde164a4b94b88027a3d03f7698"
        url = reverse("api:event_store", args=[self.project.id]) + params
        res = self.client.post(url, self.event, content_type="application/json")
        self.assertEqual(res.status_code, 401)

    def test_error_event(self):
        data = self.get_json_data("events/test_data/py_error.json")
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            self.project.issues.filter(type=IssueEventType.ERROR).count(), 1
        )
        self.assertEqual(
            IssueEvent.objects.filter(type=IssueEventType.ERROR).count(), 1
        )

    @override_settings(STRIPE_ENABLED=True, GLITCHTIP_THROTTLE_CHECK_INTERVAL=1)
    def test_check_throttle(self):
        baker.make(
            "organizations_ext.OrganizationOwner",
            organization=self.organization,
            organization_user__user__email="t@example.com",
        )
        data = self.get_json_data("events/test_data/py_error.json")
        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        # We know the throttle was checked when this simplistic lock is set
        self.assertTrue(cache.get(f"org-throttle-{self.organization.id}"))

    @override_settings(STRIPE_ENABLED=True, GLITCHTIP_THROTTLE_CHECK_INTERVAL=100000000)
    async def test_check_no_throttle(self):
        data = self.get_json_data("events/test_data/py_error.json")
        res = await self.async_client.post(
            self.url, data, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        # We know the throttle was not checked when this simplistic lock isn't set
        self.assertFalse(cache.get(f"org-throttle-{self.organization.id}"))
