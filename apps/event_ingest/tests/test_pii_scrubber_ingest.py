"""End-to-end test: PII scrubbing applied through the real envelope endpoint.

These exercise the full ingest path (HTTP -> view -> scrubber -> task ->
stored IssueEvent) so we know the per-project ``scrub_config`` actually reaches
the scrubber via the auth stored procedure, and that the persisted event is
redacted.
"""

import uuid

from django.core.cache import cache
from django.tasks import task_backends
from django.urls import reverse

from apps.issue_events.models import IssueEvent

from .utils import EventIngestTestCase, list_to_envelope


class PiiScrubberIngestTestCase(EventIngestTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.url = reverse("event_envelope", args=[self.project.id]) + self.params

    def _envelope(self, event: dict) -> str:
        event_id = uuid.uuid4().hex
        event["event_id"] = event_id
        return list_to_envelope([{"event_id": event_id}, {"type": "event"}, event])

    def _ingest(self, event: dict) -> IssueEvent:
        res = self.client.post(
            self.url, self._envelope(event), content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        task_backends["default"].flush_batches()
        self.assertEqual(IssueEvent.objects.count(), 1)
        return IssueEvent.objects.first()

    def test_scrubbing_enabled_redacts_sensitive_data(self):
        self.project.scrub_config = {"enabled": True}
        self.project.save()
        event = self._ingest(
            {
                "message": "boom",
                "level": "error",
                "extra": {"password": "hunter2", "username": "alice"},
                "request": {
                    "url": "http://example.com",
                    "headers": {"Authorization": "Bearer abc", "Accept": "json"},
                },
            }
        )
        self.assertEqual(event.data["extra"]["password"], "[Filtered]")
        self.assertEqual(event.data["extra"]["username"], "alice")
        headers = dict(event.data["request"]["headers"])
        self.assertEqual(headers["Authorization"], "[Filtered]")
        self.assertEqual(headers["Accept"], "json")

    def test_scrubbing_disabled_by_default_keeps_data(self):
        # No scrub_config and no global default -> data passes through.
        event = self._ingest(
            {
                "message": "boom",
                "level": "error",
                "extra": {"password": "hunter2"},
            }
        )
        self.assertEqual(event.data["extra"]["password"], "hunter2")

    def test_safe_keys_override(self):
        # "session" is a denylisted token, so "session_token" would normally be
        # redacted; safe_keys exempts it (token-aware), while "password" is
        # still scrubbed.
        self.project.scrub_config = {"enabled": True, "safe_keys": ["session"]}
        self.project.save()
        event = self._ingest(
            {
                "message": "boom",
                "level": "error",
                "extra": {"session_token": "keep-me", "password": "hunter2"},
            }
        )
        self.assertEqual(event.data["extra"]["session_token"], "keep-me")
        self.assertEqual(event.data["extra"]["password"], "[Filtered]")
