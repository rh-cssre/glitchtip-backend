import json
import uuid
from unittest import mock
from urllib.parse import urlparse

from django.core.cache import cache
from django.test.client import FakePayload
from django.urls import reverse
from freezegun import freeze_time

from apps.issue_events.models import IssueEvent
from apps.performance.models import TransactionEvent

from .utils import EventIngestTestCase, list_to_envelope


class EnvelopeAPITestCase(EventIngestTestCase):
    """
    These test specifically test the envelope API and act more of integration test
    Use test_process_issue_events.py for testing Event Ingest more specifically
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.url = reverse("api:event_envelope", args=[self.project.id]) + self.params
        self.django_event = self.get_json_data(
            "apps/event_ingest/tests/test_data/envelopes/django_message.json"
        )
        self.js_event = self.get_json_data(
            "apps/event_ingest/tests/test_data/envelopes/js_angular_message.json"
        )

    def get_payload(self, path, replace_id=False, set_release=None):
        """Convert JSON file into envelope format string"""
        with open(path) as json_file:
            json_data = json.load(json_file)
            if replace_id:
                new_id = uuid.uuid4().hex
                json_data[0]["event_id"] = new_id
                json_data[2]["event_id"] = new_id
            if set_release:
                json_data[0]["trace"]["release"] = set_release
                json_data[2]["release"] = set_release
            data = "\n".join([json.dumps(line) for line in json_data])
        return data

    def get_string_payload(self, json_data):
        """Convert JSON data into envelope format string"""
        return "\n".join([json.dumps(line) for line in json_data])

    def test_envelope_api(self):
        with self.assertNumQueries(18):
            res = self.client.post(
                self.url,
                list_to_envelope(self.django_event),
                content_type="application/json",
            )
        self.assertContains(res, self.django_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_envelope_api_content_type(self):
        js_payload = self.get_string_payload(self.js_event)

        res = self.client.post(
            self.url, js_payload, content_type="text/plain;charset=UTF-8"
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.js_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_accept_transaction(self):
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        # Should fail with warning about date being too old
        with mock.patch("apps.event_ingest.views.logger.warning") as mock_warning:
            res = self.client.post(
                self.url,
                data,
                content_type="application/x-sentry-envelope",
            )
            mock_warning.assert_called_once()
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TransactionEvent.objects.exists())

        with freeze_time("2020-01-01"):
            res = self.client.post(
                self.url,
                data,
                content_type="application/x-sentry-envelope",
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(TransactionEvent.objects.exists())

    def test_invalid_dsn(self):
        url = reverse("api:event_envelope", args=[self.project.id]) + "?sentry_key=aaaa"
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        res = self.client.post(
            url,
            data,
            content_type="application/x-sentry-envelope",
        )
        self.assertEqual(res.status_code, 403)

    def test_malformed_sdk_packages(self):
        event = self.django_event
        event[2]["sdk"]["packages"] = {
            "name": "cocoapods",
            "version": "just_aint_right",
        }
        res = self.client.post(
            self.url,
            list_to_envelope(event),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_nothing_event(self):
        res = self.client.post(
            self.url,
            '{}\n{"lol": "haha"}',
            content_type="application/x-sentry-envelope",
        )
        self.assertEqual(res.status_code, 200)

    @mock.patch("apps.shared.schema.utils.logger.warning")
    def test_invalid_issue_event_warning(self, mock_log):
        res = self.client.post(
            self.url,
            '{}\n{"type": "event"}\n{"timestamp": false}',
            content_type="application/x-sentry-envelope",
        )
        self.assertEqual(res.status_code, 200)
        mock_log.assert_called_once()

    def test_no_content_type(self):
        """
        Test minimal but valid event payload without a content type
        This is a unexpected but possible sdk behavior
        """
        minimal_payload = {
            "event_id": "a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0",
            "timestamp": "2025-04-08T12:00:00Z",
            "platform": "other",
        }
        data = (
            b'{"event_id": "5a337086bc1545448e29ed938729cba3"}\n{"type": "event"}\n'
            + json.dumps(minimal_payload).encode()
        )
        parsed = urlparse(self.url)  # path can be lazy
        r = {
            "PATH_INFO": self.client._get_path(parsed),
            "REQUEST_METHOD": "POST",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "CONTENT_LENGTH": str(len(data)),
            "HTTP_X_SENTRY_AUTH": f"x=x sentry_key={self.projectkey.public_key.hex}",
            "wsgi.input": FakePayload(data),
        }
        res = self.client.request(**r)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.project.issues.count(), 1)

    def test_discarded_exception(self):
        event = self.django_event
        event[2]["exception"] = {
            "values": [
                {"type": "fun", "value": "this is a fun error"},
                {"module": "", "thread_id": 1, "stacktrace": {}},
            ]
        }
        res = self.client.post(
            self.url, list_to_envelope(event), content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            IssueEvent.objects.filter(
                data__exception__values=[
                    {"type": "fun", "value": "this is a fun error"}
                ]
            ).exists()
        )

    def test_coerce_message_params(self):
        event = self.django_event
        # The ["b"] param is wrong, it should get coerced to a str
        event[2]["logentry"] = {"params": ["a", ["b"]], "message": "%s %s"}
        res = self.client.post(self.url, event, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    def test_weird_debug_meta(self):
        event = self.django_event
        # The ["b"] param is wrong, it should get coerced to a str
        event[2]["debug_meta"] = {"images": [{"type": "silly"}]}
        res = self.client.post(self.url, event, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    def test_invalid_mechanism(self):
        """
        The mechanism should not be an empty object, but the go sdk sends this
        https://github.com/getsentry/sentry-go/issues/896
        """
        event = self.django_event
        event[2]["exception"] = {
            "values": [{"type": "Error", "value": "The error", "mechanism": {}}]
        }
        res = self.client.post(self.url, event, content_type="application/json")
        self.assertEqual(res.status_code, 200)

    def test_item_with_explicit_length(self):
        """
        Verify that an envelope item with a correctly specified 'length'
        in its header is parsed and processed successfully.
        """
        payload_dict = {
            "event_id": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3",
            "timestamp": "2025-04-08T13:01:00Z",
            "platform": "python",
            "message": "Event with explicit length",
        }

        payload_bytes = json.dumps(payload_dict).encode()
        payload_length = len(payload_bytes)

        envelope_header_dict = {"event_id": payload_dict["event_id"]}
        item_header_dict = {
            "type": "event",
            "length": payload_length,
        }

        envelope_header_bytes = json.dumps(envelope_header_dict).encode()
        item_header_bytes = json.dumps(item_header_dict).encode()

        data = (
            envelope_header_bytes
            + b"\n"
            + item_header_bytes
            + b"\n"
            + payload_bytes
            + b"\n"
        )

        res = self.client.post(self.url, data, content_type="application/json")

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.project.issues.count(), 1)

    def test_envelope_ignores_unsupported_item_with_length(self):
        """
        Verify that the envelope view correctly uses the 'length' attribute
        to read and discard an unsupported item type (e.g., attachment)
        with a non-JSON payload, and then successfully processes a subsequent
        valid event item in the same envelope.
        """
        envelope_header_dict = {"sent_at": "2025-04-08T13:09:00Z"}
        envelope_header_bytes = json.dumps(envelope_header_dict).encode()

        # Unhandled data to skip
        attachment_payload_bytes = b"This is some log content.\n" + b"End."
        actual_attachment_length = len(attachment_payload_bytes)
        attachment_header_dict = {
            "type": "attachment",
            "length": actual_attachment_length,
            "filename": "debug.log",
            "content_type": "text/plain",
        }
        attachment_header_bytes = json.dumps(attachment_header_dict).encode()

        # The valid event
        event_payload_dict = {
            "event_id": "f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6",
            "timestamp": "2025-04-08T13:09:01Z",
            "platform": "java",
            "message": "Processing after ignored item",
        }
        event_payload_bytes = json.dumps(event_payload_dict).encode()
        event_payload_length = len(event_payload_bytes)
        event_header_dict = {
            "type": "event",
            "length": event_payload_length,
        }
        event_header_bytes = json.dumps(event_header_dict).encode()

        data = (
            envelope_header_bytes
            + b"\n"
            + attachment_header_bytes
            + b"\n"
            + attachment_payload_bytes
            + b"\n"
            + event_header_bytes
            + b"\n"
            + event_payload_bytes
            + b"\n"
        )

        res = self.client.post(self.url, data, content_type="application/json")

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            self.project.issues.count(),
            1,
            "Should have processed the valid event after ignoring the attachment.",
        )

    def test_envelope_ignores_log_item_with_length(self):
        """
        Ensure that log items are skipped, but subsequent valid events are being processed.
        """
        envelope_header_dict = {"sent_at": "2025-06-19T12:00:00Z"}
        envelope_header_bytes = json.dumps(envelope_header_dict).encode()

        # Log data to skip
        log_payload_bytes = b'{"msg": "some log content"}'
        log_payload_length = len(log_payload_bytes)
        log_header_dict = {
            "type": "log",
            "length": log_payload_length,
        }
        log_header_bytes = json.dumps(log_header_dict).encode()

        # Valid event
        event_payload_dict = {
            "event_id": "abcdabcdabcdabcdabcdabcdabcdabcd",
            "timestamp": "2025-04-08T13:09:01Z",
            "platform": "node",
            "message": "Logged event",
        }
        event_payload_bytes = json.dumps(event_payload_dict).encode()
        event_payload_length = len(event_payload_bytes)
        event_header_dict = {
            "type": "event",
            "length": event_payload_length,
        }
        event_header_bytes = json.dumps(event_header_dict).encode()

        data = (
            envelope_header_bytes
            + b"\n"
            + log_header_bytes
            + b"\n"
            + log_payload_bytes
            + b"\n"
            + event_header_bytes
            + b"\n"
            + event_payload_bytes
            + b"\n"
        )

        res = self.client.post(self.url, data, content_type="application/json")

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            self.project.issues.count(),
            1,
            "Should have processed the valid event after skipping the 'log' item.",
        )

    def test_long_message(self):
        event = self.django_event
        event[2]["message"] = {"formatted": "a" * 9000}
        res = self.client.post(
            self.url,
            list_to_envelope(event),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_invalid_timestamp(self):
        event = self.django_event
        event[2]["timestamp"] = "invalid"
        res = self.client.post(
            self.url,
            list_to_envelope(event),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        db_event = IssueEvent.objects.first()
        self.assertTrue(db_event)
        assert db_event.data["errors"] == [
            {
                "type": "datetime_from_date_parsing",
                "name": "timestamp",
                "value": "invalid",
            }
        ]
