import gzip
import json
import uuid
from unittest import mock
from urllib.parse import urlparse

import sentry_sdk
from django.core.cache import cache
from django.tasks import task_backends
from django.test.client import FakePayload
from django.urls import reverse
from freezegun import freeze_time

from apps.issue_events.models import IssueEvent, UserReport
from apps.performance.models import TransactionGroup

from .utils import EventIngestTestCase, list_to_envelope


class EnvelopeAPITestCase(EventIngestTestCase):
    """
    These test specifically test the envelope API and act more of integration test
    Use test_process_issue_events.py for testing Event Ingest more specifically
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.url = reverse("event_envelope", args=[self.project.id]) + self.params
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
        # TODO: re-add assertNumQueries once unit tests run on the async
        # backend. assertNumQueries only observes Django's sync connection and
        # can't count the ingest queries issued through async_connections.
        res = self.client.post(
            self.url,
            list_to_envelope(self.django_event),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()
        self.assertContains(res, self.django_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_envelope_api_gzip(self):
        """A gzip Content-Encoded envelope is decompressed + framed in Rust.

        DecompressBodyMiddleware is gone, so the view hands the raw compressed
        body and the Content-Encoding straight to gt_rust's parse_envelope.
        """
        payload = list_to_envelope(self.django_event)
        if isinstance(payload, str):
            payload = payload.encode()
        res = self.client.post(
            self.url,
            data=gzip.compress(payload),
            content_type="application/x-sentry-envelope",
            HTTP_CONTENT_ENCODING="gzip",
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.django_event[0]["event_id"])
        self.assertEqual(self.project.issues.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_envelope_api_content_type(self):
        js_payload = self.get_string_payload(self.js_event)

        res = self.client.post(
            self.url, js_payload, content_type="text/plain;charset=UTF-8"
        )
        task_backends["default"].flush_batches()
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
            task_backends["default"].flush_batches()
            mock_warning.assert_called_once()
        self.assertEqual(res.status_code, 200)
        self.assertFalse(TransactionGroup.objects.exists())

        # Fixture transaction is timestamped 2020-12-29T17:51:08Z; freeze
        # just after so it lands inside the freshness window.
        with freeze_time("2020-12-29T18:00:00Z"):
            res = self.client.post(
                self.url,
                data,
                content_type="application/x-sentry-envelope",
            )
            task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(TransactionGroup.objects.exists())

    def test_invalid_dsn(self):
        url = reverse("event_envelope", args=[self.project.id]) + "?sentry_key=aaaa"
        data = self.get_payload("events/test_data/transactions/django_simple.json")
        res = self.client.post(
            url,
            data,
            content_type="application/x-sentry-envelope",
        )
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(IssueEvent.objects.count(), 1)

    def test_nothing_event(self):
        res = self.client.post(
            self.url,
            '{}\n{"lol": "haha"}',
            content_type="application/x-sentry-envelope",
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)

    @mock.patch("apps.shared.schema.utils.logger.warning")
    def test_invalid_issue_event_warning(self, mock_log):
        res = self.client.post(
            self.url,
            '{}\n{"type": "event"}\n{"timestamp": false}',
            content_type="application/x-sentry-envelope",
        )
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)

    def test_weird_debug_meta(self):
        event = self.django_event
        # The ["b"] param is wrong, it should get coerced to a str
        event[2]["debug_meta"] = {"images": [{"type": "silly"}]}
        res = self.client.post(self.url, event, content_type="application/json")
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()

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
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            self.project.issues.count(),
            1,
            "Should have processed the valid event after ignoring the attachment.",
        )

    def test_envelope_empty_event_id_header(self):
        """
        Some SDKs (e.g. sentry-go on client_report envelopes, which have no
        associated event) send an empty string for the optional event_id
        header field instead of omitting it. The envelope must still be
        accepted rather than rejected with a 400 for an invalid UUID.
        """
        envelope_header_bytes = json.dumps(
            {
                "event_id": "",
                "sent_at": "2025-04-08T13:09:00Z",
                "dsn": "https://key@app.example.com/1",
            }
        ).encode()
        report_payload_bytes = json.dumps(
            {
                "timestamp": "2025-04-08T13:09:00Z",
                "discarded_events": [
                    {"reason": "sample_rate", "category": "transaction", "quantity": 6}
                ],
            }
        ).encode()
        report_header_bytes = json.dumps(
            {"type": "client_report", "length": len(report_payload_bytes)}
        ).encode()

        data = (
            envelope_header_bytes
            + b"\n"
            + report_header_bytes
            + b"\n"
            + report_payload_bytes
            + b"\n"
        )

        res = self.client.post(self.url, data, content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content)

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
        task_backends["default"].flush_batches()

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
        task_backends["default"].flush_batches()
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
        task_backends["default"].flush_batches()
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

    @mock.patch("django.http.HttpRequest.body", new_callable=mock.PropertyMock)
    def test_request_data_too_big(self, mock_body):
        from django.core.exceptions import RequestDataTooBig

        mock_body.side_effect = RequestDataTooBig("Payload too large")
        res = self.client.post(
            self.url,
            list_to_envelope(self.django_event),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 413)
        self.assertIn("Payload too large", res.content.decode())

    def test_accept_transaction_without_platform_defaults_to_other(self):
        """
        Transactions without a 'platform' field should be accepted and
        normalized to 'other', matching Sentry/Relay behavior.
        """
        data = self.get_json_data("events/test_data/transactions/django_simple.json")

        # Remove platform from the event payload
        # to simulate an SDK that omits it
        data[2].pop("platform", None)

        envelope = self.get_string_payload(data)

        # Fixture transaction is timestamped 2020-12-29T17:51:08Z; freeze
        # just after so it lands inside the freshness window.
        with freeze_time("2020-12-29T18:00:00Z"):
            res = self.client.post(
                self.url,
                envelope,
                content_type="application/x-sentry-envelope",
            )
            task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            TransactionGroup.objects.exists(),
            "Transaction without platform should still be ingested.",
        )

    def test_user_report_envelope(self):
        """Old SDK user_report envelope items should create a UserReport."""
        event_id = uuid.uuid4().hex
        data = "\n".join(
            [
                json.dumps({"event_id": event_id}),
                json.dumps({"type": "user_report"}),
                json.dumps(
                    {
                        "event_id": event_id,
                        "name": "Jane",
                        "email": "jane@example.com",
                        "comments": "It broke!",
                    }
                ),
            ]
        )
        res = self.client.post(
            self.url, data, content_type="application/x-sentry-envelope"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual(report.name, "Jane")
        self.assertEqual(report.email, "jane@example.com")
        self.assertEqual(report.comments, "It broke!")
        self.assertEqual(report.project_id, self.project.id)

    def test_feedback_envelope(self):
        """New SDK feedback envelope items should create a UserReport."""
        feedback_id = uuid.uuid4().hex
        data = "\n".join(
            [
                json.dumps({"event_id": feedback_id}),
                json.dumps({"type": "feedback"}),
                json.dumps(
                    {
                        "event_id": feedback_id,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "platform": "javascript",
                        "contexts": {
                            "feedback": {
                                "message": "Great product!",
                                "contact_email": "john@test.com",
                                "name": "John",
                            }
                        },
                    }
                ),
            ]
        )
        res = self.client.post(
            self.url, data, content_type="application/x-sentry-envelope"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual(report.comments, "Great product!")
        self.assertEqual(report.email, "john@test.com")
        self.assertEqual(report.name, "John")

    def test_feedback_with_associated_event(self):
        """Feedback with associated_event_id should link to the issue."""
        # First create an issue event
        res = self.client.post(
            self.url,
            list_to_envelope(self.django_event),
            content_type="application/json",
        )
        task_backends["default"].flush_batches()
        event = IssueEvent.objects.first()

        # Now send feedback referencing that event
        feedback_id = uuid.uuid4().hex
        data = "\n".join(
            [
                json.dumps({"event_id": feedback_id}),
                json.dumps({"type": "feedback"}),
                json.dumps(
                    {
                        "event_id": feedback_id,
                        "contexts": {
                            "feedback": {
                                "message": "This error is annoying",
                                "associated_event_id": event.event_id.hex,
                            }
                        },
                    }
                ),
            ]
        )
        res = self.client.post(
            self.url, data, content_type="application/x-sentry-envelope"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual(report.issue_id, event.issue_id)
        self.assertEqual(report.comments, "This error is annoying")

    def _post_envelope_with_user(
        self, user_payload: dict | None, remote_addr: str = "142.255.29.14"
    ) -> IssueEvent:
        event_id = uuid.uuid4().hex
        event_body: dict = {
            "event_id": event_id,
            "platform": "python",
            "exception": {"values": [{"type": "X", "value": "y"}]},
        }
        if user_payload is not None:
            event_body["user"] = user_payload
        data = "\n".join(
            [
                json.dumps({"event_id": event_id}),
                json.dumps({"type": "event"}),
                json.dumps(event_body),
            ]
        )
        res = self.client.post(
            self.url,
            data,
            content_type="application/x-sentry-envelope",
            REMOTE_ADDR=remote_addr,
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200, res.content)
        return IssueEvent.objects.get_event(event_id)

    def test_envelope_overwrites_payload_ip_when_scrubbing_off(self):
        self.project.scrub_ip_addresses = False
        self.project.organization.scrub_ip_addresses = False
        self.project.organization.save()
        self.project.save()
        event = self._post_envelope_with_user(
            {"id": "u1", "ip_address": "8.8.8.8"}, remote_addr="142.255.29.14"
        )
        self.assertEqual(event.data["user"]["ip_address"], "142.255.29.14")

    def test_envelope_anonymizes_payload_ip_when_scrubbing_on(self):
        self.project.scrub_ip_addresses = True
        self.project.save()
        event = self._post_envelope_with_user(
            {"id": "u1", "ip_address": "8.8.8.8"}, remote_addr="142.255.29.14"
        )
        self.assertNotEqual(event.data["user"]["ip_address"], "8.8.8.8")
        self.assertTrue(event.data["user"]["ip_address"].startswith("142.255.29."))

    def test_envelope_sets_ip_when_payload_has_no_user(self):
        self.project.scrub_ip_addresses = False
        self.project.organization.scrub_ip_addresses = False
        self.project.organization.save()
        self.project.save()
        event = self._post_envelope_with_user(None, remote_addr="142.255.29.14")
        self.assertEqual(event.data["user"]["ip_address"], "142.255.29.14")

    def test_feedback_without_contact_info(self):
        """Feedback with no name/email should still be accepted."""
        feedback_id = uuid.uuid4().hex
        data = "\n".join(
            [
                json.dumps({"event_id": feedback_id}),
                json.dumps({"type": "feedback"}),
                json.dumps(
                    {
                        "event_id": feedback_id,
                        "contexts": {
                            "feedback": {
                                "message": "Anonymous feedback",
                            }
                        },
                    }
                ),
            ]
        )
        res = self.client.post(
            self.url, data, content_type="application/x-sentry-envelope"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual(report.comments, "Anonymous feedback")
        self.assertEqual(report.name, "")
        self.assertEqual(report.email, "")

    @mock.patch("apps.event_ingest.views.sentry_sdk.capture_exception")
    @mock.patch("apps.event_ingest.views.capture_exception")
    def test_ignored_trace_metric_item(self, mock_capture, mock_scoped_capture):
        """
        A `trace_metric` item is an explicitly ignored type: it must be
        accepted-and-dropped silently, without capturing any exception, and
        must not fail the envelope.
        """
        payload_bytes = json.dumps({"some": "metric"}).encode()
        data = (
            json.dumps({"event_id": uuid.uuid4().hex}).encode()
            + b"\n"
            + json.dumps(
                {
                    "type": "trace_metric",
                    "content_type": "application/vnd.sentry.items.trace-metric+json",
                    "length": len(payload_bytes),
                }
            ).encode()
            + b"\n"
            + payload_bytes
            + b"\n"
        )

        res = self.client.post(
            self.url, data, content_type="application/x-sentry-envelope"
        )
        task_backends["default"].flush_batches()

        self.assertEqual(res.status_code, 200, res.content)
        mock_capture.assert_not_called()
        mock_scoped_capture.assert_not_called()

    @mock.patch("apps.event_ingest.views.sentry_sdk.capture_exception")
    def test_unknown_item_type_fingerprints_per_type(self, mock_capture):
        """
        An unknown item type (in neither the supported nor ignored lists)
        captures an exception fingerprinted by the offending type, so each
        genuinely-new type surfaces as its own issue rather than folding all
        unknown types together.
        """
        captured_fingerprints = []

        def record_fingerprint(_exc):
            scope = sentry_sdk.get_current_scope()
            captured_fingerprints.append(list(scope._fingerprint))

        mock_capture.side_effect = record_fingerprint

        for item_type in ("wholly_made_up_type", "another_made_up_type"):
            data = (
                json.dumps({"event_id": uuid.uuid4().hex}).encode()
                + b"\n"
                + json.dumps({"type": item_type}).encode()
                + b"\n"
                + json.dumps({"foo": "bar"}).encode()
                + b"\n"
            )
            res = self.client.post(
                self.url, data, content_type="application/x-sentry-envelope"
            )
            self.assertEqual(res.status_code, 200, res.content)

        self.assertEqual(mock_capture.call_count, 2)
        self.assertEqual(
            captured_fingerprints,
            [
                ["envelope-unsupported-item-type", "wholly_made_up_type"],
                ["envelope-unsupported-item-type", "another_made_up_type"],
            ],
        )
