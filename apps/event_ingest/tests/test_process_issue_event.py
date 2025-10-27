import os
import shutil
import uuid

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from apps.event_ingest.tests.utils import generate_event
from apps.issue_events.constants import EventStatus, LogLevel
from apps.issue_events.models import Issue, IssueAggregate, IssueEvent, IssueHash
from apps.projects.models import IssueEventProjectHourlyStatistic
from apps.releases.models import Release
from glitchtip.utils import get_random_string

from ..process_event import process_issue_events
from ..schema import (
    CSPIssueEventSchema,
    ErrorIssueEventSchema,
    IssueEventSchema,
    IssueTaskMessage,
    SecuritySchema,
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
        with self.assertNumQueries(10):
            self.process_events([{}, {}])
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueHash.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 2)
        self.assertEqual(
            IssueHash.objects.first().value.hex,
            IssueEvent.objects.first().hashes[0],
            "Hash should be stored on event",
        )
        self.assertTrue(
            IssueEventProjectHourlyStatistic.objects.filter(
                count=2, project=self.project
            ).exists()
        )
        self.assertTrue(IssueAggregate.objects.filter(count=2).exists())

    def test_two_issues(self):
        self.process_events(
            [
                {
                    "message": "a",
                },
                {
                    "message": "b",
                },
            ]
        )
        self.assertEqual(Issue.objects.count(), 2)
        self.assertEqual(IssueHash.objects.count(), 2)
        self.assertEqual(IssueEvent.objects.count(), 2)
        self.assertTrue(
            IssueEventProjectHourlyStatistic.objects.filter(
                count=2, project=self.project
            ).exists()
        )
        self.assertEqual(Issue.objects.first().short_id, 1)
        self.assertEqual(Issue.objects.last().short_id, 2)

    def test_transaction_truncation(self):
        long_string = "x" * 201
        truncated_string = "x" * 199 + "…"

        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["culprit"] = long_string
        self.process_events(data)
        first_event = IssueEvent.objects.first()
        self.assertEqual(first_event.transaction, truncated_string)

        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["transaction"] = long_string
        self.process_events(data)
        second_event = IssueEvent.objects.last()
        self.assertEqual(second_event.transaction, truncated_string)

    def test_message_empty_param_list(self):
        self.process_events(
            [
                {"logentry": {"message": "This is a warning: %s", "params": []}},
            ]
        )
        self.assertEqual(
            IssueEvent.objects.first().data["logentry"]["message"],
            "This is a warning: %s",
        )

    def test_query_release_environment_difs(self):
        """Test efficiency of existing release/environment/dif"""
        project2 = baker.make("projects.Project", organization=self.organization)
        release = baker.make("releases.Release", version="r", projects=[self.project])
        environment = baker.make(
            "environments.Environment", name="e", projects=[self.project]
        )
        baker.make("difs.DebugInformationFile", project=self.project)
        baker.make("releases.Release", projects=[self.project, project2])
        baker.make("releases.Release", version="r", projects=[project2])
        baker.make("releases.Release", version="r")
        baker.make("environments.Environment", projects=[self.project])
        baker.make("difs.DebugInformationFile", project=self.project)
        event1 = {
            "release": release.version,
            "environment": environment.name,
        }
        event2 = {
            "release": "newr",
            "environment": "newe",
        }
        with self.assertNumQueries(16):
            self.process_events([event1, {}])
        self.process_events([event1, event2, {}])
        self.assertEqual(self.project.releases.count(), 3)
        self.assertEqual(self.project.environment_set.count(), 3)

    def test_reopen_resolved_issue(self):
        event = self.process_events({})[0]
        issue = Issue.objects.first()
        issue.status = EventStatus.RESOLVED
        issue.save()
        self.process_events(event.dict())
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)

    def test_fingerprint(self):
        data = {
            "exception": [
                {
                    "type": "a",
                    "value": "a",
                }
            ],
            "event_id": uuid.uuid4(),
            "fingerprint": ["foo", None, "bar"],
        }
        self.process_events(data)

        data["exception"][0]["type"] = "lol"
        data["event_id"] = uuid.uuid4()
        self.process_events(data)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IssueEvent.objects.count(), 2)

    def test_event_release(self):
        data = self.get_json_data("events/test_data/py_hi_event.json")

        baker.make("releases.Release", version=data.get("release"))

        self.process_events(data)

        event = IssueEvent.objects.first()
        self.assertTrue(event.release)
        self.assertTrue(
            Release.objects.filter(
                version=data.get("release"), projects=self.project
            ).exists()
        )

    def test_event_release_blank(self):
        """In the SDK, it's possible to set a release to a blank string"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["release"] = ""
        self.process_events(data)
        self.assertTrue(IssueEvent.objects.first())

    def test_event_environment(self):
        # Some noise to test queries
        baker.make("environments.Environment", organization=self.organization)
        baker.make("environments.EnvironmentProject", project=self.project)

        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["environment"] = "dev"
        self.process_events(data)

        event = IssueEvent.objects.first()
        self.assertTrue(event.issue.project.environment_set.filter(name="dev").exists())
        self.assertEqual(event.issue.project.environment_set.count(), 2)

        data["event_id"] = uuid.uuid4().hex
        self.process_events(data)
        self.assertEqual(event.issue.project.environment_set.count(), 2)

    def test_multi_org_event_environment_processing(self):
        environment = baker.make(
            "environments.Environment", organization=self.organization, name="prod"
        )
        baker.make(
            "environments.EnvironmentProject",
            environment=environment,
            project=self.project,
        )

        event_list = []
        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["environment"] = "dev"
        event_list.append(
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id,
                payload=IssueEventSchema(**data),
                received=timezone.now(),
            )
        )

        org_b = baker.make("organizations_ext.organization")
        org_b_project = baker.make("projects.Project", organization=org_b)

        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["environment"] = "prod"
        event_list.append(
            IssueTaskMessage(
                project_id=org_b_project.id,
                organization_id=org_b.id,
                payload=IssueEventSchema(**data),
                received=timezone.now(),
            )
        )

        process_issue_events(event_list)

        self.assertTrue(self.project.environment_set.filter(name="dev").exists())
        self.assertEqual(self.project.environment_set.count(), 2)

        self.assertTrue(org_b_project.environment_set.filter(name="prod").exists())
        self.assertEqual(org_b_project.environment_set.count(), 1)

    def test_multi_org_event_release_processing(self):
        release = baker.make(
            "releases.Release", organization=self.organization, version="v1.0"
        )
        baker.make(
            "releases.ReleaseProject",
            release=release,
            project=self.project,
        )

        event_list = []
        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["release"] = "v2.0"
        event_list.append(
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id,
                payload=IssueEventSchema(**data),
                received=timezone.now(),
            )
        )

        org_b = baker.make("organizations_ext.organization")
        org_b_project = baker.make("projects.Project", organization=org_b)

        data = self.get_json_data("events/test_data/py_hi_event.json")
        data["release"] = "v1.0"
        event_list.append(
            IssueTaskMessage(
                project_id=org_b_project.id,
                organization_id=org_b.id,
                payload=IssueEventSchema(**data),
                received=timezone.now(),
            )
        )

        process_issue_events(event_list)

        self.assertTrue(self.organization.release_set.filter(version="v2.0").exists())
        self.assertEqual(self.organization.release_set.count(), 2)

        self.assertTrue(org_b.release_set.filter(version="v1.0").exists())
        self.assertEqual(org_b.release_set.count(), 1)

    def test_process_sourcemap(self):
        sample_event = {
            "exception": {
                "values": [
                    {
                        "type": "Error",
                        "value": "The error",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "http://localhost:8080/dist/bundle.js",
                                    "function": "?",
                                    "in_app": True,
                                    "lineno": 2,
                                    "colno": 74016,
                                },
                                {
                                    "filename": "http://localhost:8080/dist/bundle.js",
                                    "function": "?",
                                    "in_app": True,
                                    "lineno": 2,
                                    "colno": 74012,
                                },
                                {
                                    "filename": "http://localhost:8080/dist/bundle.js",
                                    "function": "?",
                                    "in_app": True,
                                    "lineno": 2,
                                    "colno": 73992,
                                },
                            ]
                        },
                        "mechanism": {"type": "onerror", "handled": False},
                    }
                ]
            },
            "level": "error",
            "platform": "javascript",
            "event_id": "0691751a89db419994efac8ac9b00a5d",
            "timestamp": 1648414309.82,
            "environment": "production",
            "request": {
                "url": "http://localhost:8080/",
                "headers": {
                    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:98.0) Gecko/20100101 Firefox/98.0"
                },
            },
        }
        release = baker.make("releases.Release", organization=self.organization)
        release.projects.add(self.project)
        blob_bundle = baker.make("files.FileBlob", blob="uploads/file_blobs/bundle.js")
        blob_bundle_map = baker.make(
            "files.FileBlob", blob="uploads/file_blobs/bundle.js.map"
        )
        baker.make(
            "sourcecode.DebugSymbolBundle",
            organization=self.organization,
            release=release,
            file__name="bundle.js",
            file__blob=blob_bundle,
            sourcemap_file__name="bundle.js.map",
            sourcemap_file__blob=blob_bundle_map,
        )
        try:
            os.mkdir("./uploads/file_blobs")
        except FileExistsError:
            pass
        shutil.copyfile(
            "./apps/event_ingest/tests/test_data/bundle.js",
            "./uploads/file_blobs/bundle.js",
        )
        shutil.copyfile(
            "./apps/event_ingest/tests/test_data/bundle.js.map",
            "./uploads/file_blobs/bundle.js.map",
        )
        data = sample_event | {"release": release.version}

        self.process_events(data)
        # Show that colno changes
        self.assertEqual(
            IssueEvent.objects.first().data["exception"]["values"][0]["stacktrace"][
                "frames"
            ][0]["colno"],
            13,
        )
        # Show that pre and post context is included
        self.assertEqual(
            len(
                IssueEvent.objects.first().data["exception"]["values"][0]["stacktrace"][
                    "frames"
                ][0]["pre_context"]
            ),
            5,
        )
        self.assertEqual(
            len(
                IssueEvent.objects.first().data["exception"]["values"][0]["stacktrace"][
                    "frames"
                ][0]["post_context"]
            ),
            1,
        )

        self.assertTrue(IssueEvent.objects.filter(release=release).exists())

    def test_search_vector(self):
        word = "orange"
        for _ in range(2):
            self.process_events([{"message": word}])
        issue = Issue.objects.filter(search_vector=word).first()
        self.assertTrue(issue)
        self.assertEqual(len(issue.search_vector.split(" ")), 1)

    @override_settings(SEARCH_MAX_LEXEMES=3)
    def test_search_vector_truncate(self):
        """Trucate both max lexemes and size of each lexeme"""
        events = [
            {
                "message": get_random_string(),
                "fingerprint": ["79054025255fb1a26e4bc422aef54eb4"],
            }
            for _ in range(4)
        ]
        self.process_events(events)
        issue = Issue.objects.get()
        self.assertEqual(
            len(issue.search_vector.split(" ")), 3, "truncate number of lexemes"
        )

    def test_search_vector_content(self):
        event_data = generate_event()
        event = IssueTaskMessage(
            organization_id=self.organization.id,
            project_id=self.project.id,
            payload=ErrorIssueEventSchema(**event_data),
            received=timezone.now(),
        )
        process_issue_events([event])
        file_name = event_data["exception"]["values"][0]["stacktrace"]["frames"][0][
            "filename"
        ]
        issue_event = IssueEvent.objects.get(pk=event.payload.event_id)
        self.assertIn(file_name, issue_event.issue.search_vector)
        self.assertIn(
            event_data["request"]["url"].split("//")[-1],
            issue_event.issue.search_vector,
        )

    def test_null_character_event(self):
        """
        Unicode null characters \u0000 are not supported by Postgres JSONB
        NUL \x00 characters are not supported by Postgres string types
        They should be filtered out
        """
        data = self.get_json_data("events/test_data/py_error.json")
        data["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] = (
            "a\u0000a"
        )
        data["exception"]["values"][0]["value"] = "\x00\u0000"
        self.process_events(data)

    def test_csp_event_processing(self):
        self.create_logged_in_user()
        payload = self.get_json_data(
            "apps/event_ingest/tests/test_data/csp/mozilla_example.json"
        )
        data = SecuritySchema(**payload)
        event = CSPIssueEventSchema(csp=data.csp_report.dict(by_alias=True))
        process_issue_events(
            [
                IssueTaskMessage(
                    project_id=self.project.id,
                    organization_id=self.organization.id,
                    payload=event.dict(by_alias=True),
                    received=timezone.now(),
                )
            ]
        )
        issue = Issue.objects.get()
        url = reverse("api:get_latest_issue_event", kwargs={"issue_id": issue.id})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["culprit"], "style-src-elem")

    def test_project_throttle_rate(self):
        self.project.event_throttle_rate = 100
        self.project.save()
        event_store_url = (
            reverse("api:event_store", args=[self.project.id])
            + "?sentry_key="
            + self.project.projectkey_set.first().public_key.hex
        )
        res = self.client.post(
            event_store_url,
            {"fake": "data"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 429)


class SentryCompatTestCase(EventIngestTestCase):
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
            return value[:-3]
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
        event = IssueTaskMessage(
            organization_id=self.organization.id if self.organization else None,
            project_id=self.project.id,
            payload=event_class(**event_data),
            received=timezone.now(),
        )
        process_issue_events([event])
        return IssueEvent.objects.get(pk=event.payload.event_id)

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

    def test_ruby_zero_division(self):
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "ruby_zero_division"
        )

        event = self.submit_event(sdk_error)
        event_json = self.get_event_json(event)
        res = self.client.get(self.get_project_events_detail(event.pk))
        res_data = res.json()
        res_exception = next(filter(is_exception, res_data["entries"]), None)
        sentry_exception = next(filter(is_exception, sentry_data["entries"]), None)
        self.assertEqual(
            res_exception["data"]["values"][0]["stacktrace"]["frames"][-1]["context"],
            sentry_exception["data"]["values"][0]["stacktrace"]["frames"][-1][
                "context"
            ],
        )

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
                "userReport",
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

    def test_js_error_with_context(self):
        self.project.scrub_ip_addresses = False
        self.project.save()
        sdk_error, sentry_json, sentry_data = self.get_json_test_data(
            "js_error_with_context"
        )
        event_store_url = (
            reverse("api:event_store", args=[self.project.id])
            + "?sentry_key="
            + self.project.projectkey_set.first().public_key.hex
        )
        res = self.client.post(
            event_store_url,
            sdk_error,
            content_type="application/json",
            REMOTE_ADDR="142.255.29.14",
        )
        res_data = res.json()
        event = IssueEvent.objects.get(pk=res_data["event_id"])
        event_json = self.get_event_json(event)
        self.assertCompareData(event_json, sentry_json, ["title", "extra", "user"])

        url = self.get_project_events_detail(event.pk)
        res = self.client.get(url)
        res_json = res.json()
        self.assertCompareData(res_json, sentry_data, ["context"])
        self.assertCompareData(
            res_json["user"], sentry_data["user"], ["id", "email", "ip_address"]
        )

    def test_small_js_error(self):
        """A small example to test stacktraces"""
        sdk_error, sentry_json, sentry_data = self.get_json_test_data("small_js_error")
        event = self.submit_event(sdk_error, event_type="default")
        event_json = self.get_event_json(event)
        self.assertCompareData(
            event_json["exception"]["values"][0],
            sentry_json["exception"]["values"][0],
            ["type", "values", "exception", "abs_path"],
        )

    def test_bad_message_format(self):
        """%d will not accept a string, it should fallback to not formatting"""
        event = generate_event()
        event["message"] = {"message": "lol %d", "params": ["a"]}
        result = self.submit_event(event)
        self.assertEqual(result.data["logentry"]["formatted"], "")

        event = generate_event()
        event["message"] = {"message": "lol %d", "params": [1]}
        result = self.submit_event(event)
        self.assertEqual(result.data["logentry"]["formatted"], "lol 1")

    def test_invalid_user(self):
        """User interface may contain some arbitrary data"""
        event = generate_event()
        event["user"] = {"username": {"a": "b"}}
        result = self.submit_event(event)
        self.assertEqual(result.data.get("user"), None)

        event = generate_event()
        event["user"] = {"username": "user", "subscription": {"isActive": True}}
        result = self.submit_event(event)
        self.assertEqual(result.data["user"]["username"], "user")

    def test_large_tag(self):
        """User interface may contain some arbitrary data"""
        event = generate_event()
        long_tag_value = "a" * 300
        event["tags"] = {
            "key_with_long_value": long_tag_value,
            "long_key_" + "b" * 280: "normal_value",
        }
        self.submit_event(event)
