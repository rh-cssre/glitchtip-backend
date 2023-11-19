import uuid

from django.urls import reverse

from apps.issue_events.constants import EventStatus
from apps.issue_events.models import Issue, IssueEvent, IssueHash

from ..process_event import process_issue_events
from ..schema import (
    ErrorIssueEventSchema,
    InterchangeIssueEvent,
    IssueEventSchema,
)
from .utils import EventIngestTestCase

COMPAT_TEST_DATA_DIR = "events/test_data"


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

    def submit_event(self, event_data: dict) -> IssueEvent:
        event = InterchangeIssueEvent(
            event_id=event_data["event_id"],
            project_id=self.project.id,
            payload=ErrorIssueEventSchema(**event_data),
        )
        process_issue_events([event])
        return IssueEvent.objects.get(pk=event.event_id)

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

        # event_json = self.get_event_json(event)
        # self.assertCompareData(event_json, sentry_json, ["datetime", "breadcrumbs"])

        # url = self.get_project_events_detail(event.pk)
        # res = self.client.get(url)
        # res_data = res.json()
        # self.assertCompareData(res_data, sentry_data, ["datetime"])
        # self.assertEqual(res_data["entries"][1].get("type"), "breadcrumbs")
        # self.assertEqual(
        #     res_data["entries"][1],
        #     self.upgrade_data(sentry_data["entries"][1]),
        # )
