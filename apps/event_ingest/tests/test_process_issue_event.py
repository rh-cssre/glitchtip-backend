import uuid

from django.urls import reverse

from apps.issue_events.constants import EventStatus, LogLevel
from apps.issue_events.models import Issue, IssueEvent, IssueHash

from ..process_event import process_issue_events
from ..schema import (
    ErrorIssueEventSchema,
    InterchangeIssueEvent,
    IssueEventSchema,
)
from .utils import EventIngestTestCase

COMPAT_TEST_DATA_DIR = "events/test_data"


def is_exception(v):
    return v.get("type") == "exception"


class IssueEventIngestTestCase(EventIngestTestCase):
    """
    These tests bypass the API and celery. They test the event ingest logic itself.
    This file should be large are test the following use cases
    - Multiple event saved at the same time
    - Sentry API compatibility
    - Default, Error, and CSP types
    - Graceful failure such as duplicate event ids or invalid data
    """

    def test_two_events(self):
        events = []
        for _ in range(2):
            payload = IssueEventSchema()
            events.append(
                InterchangeIssueEvent(project_id=self.project.id, payload=payload)
            )
        with self.assertNumQueries(12):
            process_issue_events(events)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueHash.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 2)

    def test_reopen_resolved_issue(self):
        event = InterchangeIssueEvent(
            project_id=self.project.id, payload=IssueEventSchema()
        )
        process_issue_events([event])
        issue = Issue.objects.first()
        issue.status = EventStatus.RESOLVED
        issue.save()
        event.event_id = uuid.uuid4().hex
        process_issue_events([event])
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)


class SentryCompatTestCase(IssueEventIngestTestCase):
    """
    These tests specifically test former open source sentry compatibility
    But otherwise are part of issue event ingest testing
    """

    def setUp(self):
        super().setUp()
        self.create_logged_in_user()

    def get_json_test_data(self, name: str):
        """Get incoming event, sentry json, sentry api event"""
        event = self.get_json_data(
            f"{COMPAT_TEST_DATA_DIR}/incoming_events/{name}.json"
        )
        sentry_json = self.get_json_data(
            f"{COMPAT_TEST_DATA_DIR}/oss_sentry_json/{name}.json"
        )
        # Force captured test data to match test generated data
        sentry_json["project"] = self.project.id
        api_sentry_event = self.get_json_data(
            f"{COMPAT_TEST_DATA_DIR}/oss_sentry_events/{name}.json"
        )
        return event, sentry_json, api_sentry_event

    def get_event_json(self, event: IssueEvent):
        return self.client.get(
            reverse(
                "api:get_event_json",
                kwargs={
                    "organization_slug": self.organization.slug,
                    "issue_id": event.issue_id,
                    "event_id": event.id,
                },
            )
        ).json()

    # Upgrade functions handle intentional differences between GlitchTip and Sentry OSS
    def upgrade_title(self, value: str):
        """Sentry OSS uses ... while GlitchTip uses unicode …"""
        if value[-1] == "…":
            return value[:-4]
        return value.strip("...")

    def upgrade_metadata(self, value: dict):
        value["title"] = self.upgrade_title(value["title"])
        return value

    def assertCompareData(self, data1: dict, data2: dict, fields: list[str]):
        """Compare data of two dict objects. Compare only provided fields list"""
        for field in fields:
            field_value1 = data1.get(field)
            field_value2 = data2.get(field)
            if field == "datetime":
                # Check that it's close enough
                field_value1 = field_value1[:23]
                field_value2 = field_value2[:23]
            if field == "title" and isinstance(field_value1, str):
                field_value1 = self.upgrade_title(field_value1)
                if field_value2:
                    field_value2 = self.upgrade_title(field_value2)
            if (
                field == "metadata"
                and isinstance(field_value1, dict)
                and field_value1.get("title")
            ):
                field_value1 = self.upgrade_metadata(field_value1)
                if field_value2:
                    field_value2 = self.upgrade_metadata(field_value2)
            self.assertEqual(
                field_value1,
                field_value2,
                f"Failed for field '{field}'",
            )

    def get_project_events_detail(self, event_id: str):
        return reverse(
            "api:get_project_issue_event",
            kwargs={
                "organization_slug": self.project.organization.slug,
                "project_slug": self.project.slug,
                "event_id": event_id,
            },
        )

    def submit_event(self, event_data: dict, event_type="error") -> IssueEvent:
        event_class = ErrorIssueEventSchema

        if event_type == "default":
            event_class = IssueEventSchema
        event = InterchangeIssueEvent(
            event_id=event_data["event_id"],
            project_id=self.project.id,
            payload=event_class(**event_data),
        )
        process_issue_events([event])
        return IssueEvent.objects.get(pk=event.event_id)

    def upgrade_data(self, data):
        """A recursive replace function"""
        if isinstance(data, dict):
            return {k: self.upgrade_data(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.upgrade_data(i) for i in data]
        return data

    def test_template_error(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "django_template_error"
        )
        event = self.submit_event(sdk_error)

        url = self.get_project_events_detail(event.id.hex)
        res = self.client.get(url)
        res_data = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertCompareData(res_data, sentry_data, ["culprit", "title", "metadata"])
        res_frames = res_data["entries"][0]["data"]["values"][0]["stacktrace"]["frames"]
        frames = sentry_data["entries"][0]["data"]["values"][0]["stacktrace"]["frames"]

        for i in range(6):
            # absPath don't always match - needs fixed
            self.assertCompareData(res_frames[i], frames[i], ["absPath"])
        for res_frame, frame in zip(res_frames, frames):
            self.assertCompareData(
                res_frame,
                frame,
                ["lineNo", "function", "filename", "module", "context"],
            )
            if frame.get("vars"):
                self.assertCompareData(
                    res_frame["vars"], frame["vars"], ["exc", "request"]
                )
                if frame["vars"].get("get_response"):
                    # Memory address is different, truncate it
                    self.assertEqual(
                        res_frame["vars"]["get_response"][:-16],
                        frame["vars"]["get_response"][:-16],
                    )

        self.assertCompareData(
            res_data["entries"][0]["data"],
            sentry_data["entries"][0]["data"],
            ["env", "headers", "url", "method", "inferredContentType"],
        )

        url = reverse("api:get_issue", kwargs={"issue_id": event.issue.pk})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()

        data = self.get_json_data("events/test_data/django_template_error_issue.json")
        self.assertCompareData(res_data, data, ["title", "metadata"])

    def test_js_sdk_with_unix_timestamp(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "js_event_with_unix_timestamp"
        )
        event = self.submit_event(sdk_error)
        self.assertNotEqual(event.timestamp, sdk_error["timestamp"])
        self.assertEqual(event.timestamp.year, 2020)

        event_json = self.get_event_json(event)
        self.assertCompareData(event_json, sentry_json, ["datetime"])

        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        self.assertCompareData(res_data, sentry_data, ["timestamp"])
        self.assertEqual(res_data["entries"][1].get("type"), "breadcrumbs")
        self.maxDiff = None
        self.assertEqual(
            res_data["entries"][1],
            self.upgrade_data(sentry_data["entries"][1]),
        )

    def test_dotnet_error(self):
        sdk_error = self.get_json_data(
            "events/test_data/incoming_events/dotnet_error.json"
        )
        event = self.submit_event(sdk_error)
        self.assertEqual(IssueEvent.objects.count(), 1)

        sentry_data = self.get_json_data(
            "events/test_data/oss_sentry_events/dotnet_error.json"
        )
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        self.assertCompareData(
            res_data,
            sentry_data,
            ["eventID", "title", "culprit", "platform", "type", "metadata"],
        )
        res_exception = next(filter(is_exception, res_data["entries"]), None)
        sentry_exception = next(filter(is_exception, sentry_data["entries"]), None)
        self.assertEqual(
            res_exception["data"].get("hasSystemFrames"),
            sentry_exception["data"].get("hasSystemFrames"),
        )

    def test_php_message_event(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "php_message_event"
        )
        event = self.submit_event(sdk_error)

        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        self.assertCompareData(
            res_data,
            sentry_data,
            [
                "message",
                "title",
            ],
        )
        self.assertEqual(
            res_data["entries"][0]["data"]["params"],
            sentry_data["entries"][0]["data"]["params"],
        )

    def test_django_message_params(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "django_message_params"
        )
        event = self.submit_event(sdk_error)
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        self.assertCompareData(
            res_data,
            sentry_data,
            [
                "message",
                "title",
            ],
        )
        self.assertEqual(res_data["entries"][0], sentry_data["entries"][0])

    def test_message_event(self):
        """A generic message made with the Sentry SDK. Generally has less data than exceptions."""
        from events.test_data.django_error_factory import message

        event = self.submit_event(message, event_type="default")
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        data = self.get_json_data("events/test_data/django_message_event.json")
        self.assertCompareData(
            res_data,
            data,
            ["title", "culprit", "type", "metadata", "platform", "packages"],
        )

    def test_python_logging(self):
        """Test Sentry SDK logging integration based event"""
        sdk_error, sentry_json, sentry_data = self.get_json_test_data("python_logging")
        event = self.submit_event(sdk_error, event_type="default")

        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        self.assertEqual(res.status_code, 200)
        self.assertCompareData(
            res_data,
            sentry_data,
            [
                "title",
                "logentry",
                "culprit",
                "type",
                "metadata",
                "platform",
                "packages",
            ],
        )

    def test_go_file_not_found(self):
        sdk_error = self.get_json_data(
            "events/test_data/incoming_events/go_file_not_found.json"
        )
        event = self.submit_event(sdk_error)

        sentry_data = self.get_json_data(
            "events/test_data/oss_sentry_events/go_file_not_found.json"
        )
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertCompareData(
            res_data,
            sentry_data,
            ["title", "culprit", "type", "metadata", "platform"],
        )

    def test_very_small_event(self):
        """
        Shows a very minimalist event example. Good for seeing what data is null
        """
        sdk_error = self.get_json_data(
            "events/test_data/incoming_events/very_small_event.json"
        )
        event = self.submit_event(sdk_error, event_type="default")

        sentry_data = self.get_json_data(
            "events/test_data/oss_sentry_events/very_small_event.json"
        )
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertCompareData(
            res_data,
            sentry_data,
            ["culprit", "type", "platform", "entries"],
        )

    def test_python_zero_division(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "python_zero_division"
        )
        event = self.submit_event(sdk_error)
        event_json = self.get_event_json(event)
        self.assertCompareData(
            event_json,
            sentry_json,
            [
                "event_id",
                "project",
                "release",
                "dist",
                "platform",
                "level",
                "modules",
                "time_spent",
                "sdk",
                "type",
                "title",
                "breadcrumbs",
            ],
        )
        self.assertCompareData(
            event_json["request"],
            sentry_json["request"],
            [
                "url",
                "headers",
                "method",
                "env",
                "query_string",
            ],
        )
        self.assertEqual(
            event_json["datetime"][:22],
            sentry_json["datetime"][:22],
            "Compare if datetime is almost the same",
        )

        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertCompareData(
            res_data,
            sentry_data,
            ["title", "culprit", "type", "metadata", "platform", "packages"],
        )
        self.assertCompareData(
            res_data["entries"][1]["data"],
            sentry_data["entries"][1]["data"],
            [
                "inferredContentType",
                "env",
                "headers",
                "url",
                "query",
                "data",
                "method",
            ],
        )
        issue = event.issue
        issue.refresh_from_db()
        self.assertEqual(issue.level, LogLevel.ERROR)

    def test_dotnet_zero_division(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "dotnet_divide_zero"
        )
        event = self.submit_event(sdk_error)
        event_json = self.get_event_json(event)
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        self.assertCompareData(event_json, sentry_json, ["environment"])
        self.assertCompareData(
            res_data,
            sentry_data,
            [
                "eventID",
                "title",
                "culprit",
                "platform",
                "type",
                "metadata",
            ],
        )
        res_exception = next(filter(is_exception, res_data["entries"]), None)
        sentry_exception = next(filter(is_exception, sentry_data["entries"]), None)
        self.assertEqual(
            res_exception["data"]["values"][0]["stacktrace"]["frames"][4]["context"],
            sentry_exception["data"]["values"][0]["stacktrace"]["frames"][4]["context"],
        )
        tags = res_data.get("tags")
        browser_tag = next(filter(lambda tag: tag["key"] == "browser", tags), None)
        self.assertEqual(browser_tag["value"], "Firefox 76.0")
        environment_tag = next(
            filter(lambda tag: tag["key"] == "environment", tags), None
        )
        self.assertEqual(environment_tag["value"], "Development")

    def test_sentry_cli_send_event_no_level(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "sentry_cli_send_event_no_level"
        )
        event = self.submit_event(sdk_error, event_type="default")
        event_json = self.get_event_json(event)

        self.assertCompareData(event_json, sentry_json, ["title"])
        self.assertEqual(event_json["project"], event.issue.project_id)

        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()

        self.assertCompareData(
            res_data,
            sentry_data,
            [
                # "userReport",
                "title",
                "culprit",
                "type",
                "metadata",
                "message",
                "platform",
                "previousEventID",
            ],
        )
        self.assertEqual(res_data["projectID"], event.issue.project_id)

    # def test_js_error_with_context(self):
    #     self.project.scrub_ip_addresses = False
    #     self.project.save()
    #     sdk_error, sentry_json, sentry_data = self.get_json_test_data(
    #         "js_error_with_context"
    #     )
    #     res = self.client.post(
    #         self.event_store_url, sdk_error, format="json", REMOTE_ADDR="142.255.29.14"
    #     )
    #     event = Event.objects.get(pk=res.data["id"])
    #     event_json = event.event_json()
    #     self.assertCompareData(
    #         event_json, sentry_json, ["title", "message", "extra", "user"]
    #     )

    #     url = self.get_project_events_detail(event.pk)
    #     res = self.client.get(url)
    #     self.assertCompareData(res.json(), sentry_data, ["context", "user"])

    # def test_elixir_stacktrace(self):
    #     """The elixir SDK does things differently"""
    #     sdk_error, sentry_json, sentry_data = self.get_json_test_data("elixir_error")
    #     res = self.client.post(self.event_store_url, sdk_error, format="json")
    #     event = Event.objects.get(pk=res.data["id"])
    #     event_json = event.event_json()
    #     self.assertCompareData(
    #         event_json["exception"]["values"][0],
    #         sentry_json["exception"]["values"][0],
    #         ["type", "values", "exception"],
    #     )

    # def test_small_js_error(self):
    #     """A small example to test stacktraces"""
    #     sdk_error, sentry_json, sentry_data = self.get_json_test_data("small_js_error")
    #     res = self.client.post(self.event_store_url, sdk_error, format="json")
    #     event = Event.objects.get(pk=res.data["id"])
    #     event_json = event.event_json()
    #     self.assertCompareData(
    #         event_json["exception"]["values"][0],
    #         sentry_json["exception"]["values"][0],
    #         ["type", "values", "exception", "abs_path"],
    #     )
