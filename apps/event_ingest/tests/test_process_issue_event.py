import json
import os
import shutil
import tempfile
import uuid
import zipfile
from hashlib import sha1

from django.core.files import File as DjangoFile
from django.tasks import task_backends
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from symbolic import Archive, normalize_debug_id

from apps.difs.tasks import event_difs_resolve_stacktrace
from apps.event_ingest.tests.utils import generate_event
from apps.files.models import FileBlob
from apps.issue_events.constants import EventStatus, LogLevel
from apps.issue_events.models import (
    Issue,
    IssueAggregate,
    IssueEvent,
    IssueHash,
    IssueIndex,
)
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
from .utils import EventIngestTestCase, run_async_closing


def _process_issue_events(*args, **kwargs):
    return run_async_closing(process_issue_events, *args, **kwargs)


COMPAT_TEST_DATA_DIR = "events/test_data"


def is_exception(v):
    return v.get("type") == "exception"


class IssueEventIngestTestCase(EventIngestTestCase):
    """
    These tests bypass the API and task queue. They test the event ingest logic itself.
    This file should be large are test the following use cases
    - Multiple event saved at the same time
    - Sentry API compatibility
    - Default, Error, and CSP types
    - Graceful failure such as duplicate event ids or invalid data
    """

    def test_two_events(self):
        # TODO: re-add assertNumQueries once unit tests run on the async
        # backend. assertNumQueries only observes Django's sync connection and
        # can't count the ingest queries issued through async_connections.
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
        # TODO: re-add assertNumQueries once unit tests run on the async
        # backend. assertNumQueries only observes Django's sync connection and
        # can't count the ingest queries issued through async_connections.
        self.process_events([event1, {}])
        self.process_events([event1, event2, {}])
        self.assertEqual(self.project.releases.count(), 3)
        self.assertEqual(self.project.environment_set.count(), 3)

    def test_reopen_resolved_issue(self):
        event = self.process_events({})[0]
        issue = Issue.objects.first()
        IssueIndex.objects.filter(issue=issue).update(status=EventStatus.RESOLVED)
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

    def test_first_release_set_on_new_issue(self):
        """first_release should be set when a new issue is created with a release"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        self.assertIsNotNone(issue.first_release)
        release_version = data.get("release")
        self.assertEqual(issue.first_release.version, release_version)

    def test_first_release_not_updated_on_second_event(self):
        """first_release should not change when a second event with a different release arrives"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        original_release = issue.first_release

        # Send a second event with the same fingerprint but a different release
        data2 = self.get_json_data("events/test_data/py_hi_event.json")
        data2["release"] = "v2.0.0"
        self.process_events(data2)

        issue.refresh_from_db()
        self.assertEqual(issue.first_release, original_release)

    def test_first_release_null_without_release(self):
        """first_release should be null when no release is provided"""
        self.process_events({})
        issue = Issue.objects.first()
        self.assertIsNone(issue.first_release)

    def test_last_release_set_on_new_issue(self):
        """last_release should be set when a new issue is created with a release"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        self.assertIsNotNone(issue.last_release)
        self.assertEqual(issue.last_release.version, data.get("release"))

    def test_last_release_updated_on_second_event(self):
        """last_release should update when a second event with a different release arrives"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        original_release = issue.last_release

        data2 = self.get_json_data("events/test_data/py_hi_event.json")
        data2["release"] = "v2.0.0"
        self.process_events(data2)

        issue.refresh_from_db()
        self.assertNotEqual(issue.last_release, original_release)
        self.assertEqual(issue.last_release.version, "v2.0.0")

    def test_last_release_not_cleared_by_event_without_release(self):
        """An event without a release should not erase last_release"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        self.assertIsNotNone(issue.last_release)

        # Send a second event with no release (same fingerprint)
        data2 = self.get_json_data("events/test_data/py_hi_event.json")
        data2.pop("release", None)
        self.process_events(data2)

        issue.refresh_from_db()
        self.assertIsNotNone(issue.last_release)

    def test_resolve_in_release_no_reopen_same_release(self):
        """Resolved issue with resolved_in_release should NOT reopen for events from same release"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        release = issue.first_release

        # Resolve in this release
        issue.resolved_in_release = release
        issue.save(update_fields=["resolved_in_release"])
        IssueIndex.objects.filter(issue=issue).update(status=EventStatus.RESOLVED)

        # Send another event with the same release
        self.process_events(data)
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.RESOLVED)

    def test_resolve_in_release_reopen_different_release(self):
        """Resolved issue with resolved_in_release should reopen for events from a different release"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()
        release = issue.first_release

        issue.resolved_in_release = release
        issue.save(update_fields=["resolved_in_release"])
        IssueIndex.objects.filter(issue=issue).update(status=EventStatus.RESOLVED)

        # Send event with a different release
        data2 = self.get_json_data("events/test_data/py_hi_event.json")
        data2["release"] = "v2.0.0"
        self.process_events(data2)

        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)
        self.assertIsNone(issue.resolved_in_release)

    def test_resolve_plain_always_reopens(self):
        """Resolved issue without resolved_in_release should reopen on any event (backward compat)"""
        data = self.get_json_data("events/test_data/py_hi_event.json")
        self.process_events(data)
        issue = Issue.objects.first()

        # Plain resolve (no resolved_in_release)
        IssueIndex.objects.filter(issue=issue).update(status=EventStatus.RESOLVED)

        self.process_events(data)
        issue.refresh_from_db()
        self.assertEqual(issue.status, EventStatus.UNRESOLVED)

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

        _process_issue_events(event_list)

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

        _process_issue_events(event_list)

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
        self.assertEqual(
            IssueEvent.objects.first().data["exception"]["values"][0]["raw_stacktrace"][
                "frames"
            ][0]["colno"],
            74016,
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

    def test_process_sourcemap_skips_raw_stacktrace_when_nothing_remaps(self):
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
                                }
                            ]
                        },
                        "mechanism": {"type": "onerror", "handled": False},
                    }
                ]
            },
            "level": "error",
            "platform": "javascript",
            "event_id": "0691751a89db419994efac8ac9b00a5e",
            "timestamp": 1648414309.82,
            "environment": "production",
            "request": {
                "url": "http://localhost:8080/",
                "headers": {
                    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:98.0) Gecko/20100101 Firefox/98.0"
                },
            },
        }
        baker.make(
            "sourcecode.DebugSymbolBundle",
            organization=self.organization,
            release=baker.make("releases.Release", organization=self.organization),
            file__name="other.js",
            sourcemap_file__name="other.js.map",
        )
        self.process_events(sample_event)
        self.assertNotIn(
            "raw_stacktrace",
            IssueEvent.objects.first().data["exception"]["values"][0],
        )

    def test_search_vector(self):
        word = "orange"
        for _ in range(2):
            self.process_events([{"message": word}])
        # Full-text search now lives in IssueIndex, not Issue.search_vector.
        issue = Issue.objects.filter(index__fts_document=word).first()
        self.assertTrue(issue)
        document = IssueIndex.objects.get(issue=issue).fts_document
        self.assertEqual(len(document.split(" ")), 1)

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
        document = IssueIndex.objects.get(issue=issue).fts_document
        self.assertEqual(len(document.split(" ")), 3, "truncate number of lexemes")

    def test_search_vector_content(self):
        event_data = generate_event()
        event = IssueTaskMessage(
            organization_id=self.organization.id,
            project_id=self.project.id,
            payload=ErrorIssueEventSchema(**event_data),
            received=timezone.now(),
        )
        _process_issue_events([event])
        file_name = event_data["exception"]["values"][0]["stacktrace"]["frames"][0][
            "filename"
        ]
        issue_event = IssueEvent.objects.get_event(event.payload.event_id)
        document = IssueIndex.objects.get(issue=issue_event.issue).fts_document
        self.assertIn(file_name, document)
        self.assertIn(
            event_data["request"]["url"].split("//")[-1],
            document,
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
        _process_issue_events(
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

    def create_source_bundle_with_debug_symbols(self, debug_id):
        source_code = """import SwiftUI

struct ContentView: View {
    var body: some View {
        Button("Trigger Error") {
            triggerError()
        }
    }

    func triggerError() {
        throw NSError(domain: "TestError", code: 1)
    }
}
"""
        file_path = "/Users/test/ContentView.swift"

        manifest = {
            "files": {
                f"files{file_path}": {
                    "type": "source",
                    "path": file_path,
                }
            },
            "arch": "arm64",
            "code_id": "testcodeid123",
            "debug_id": debug_id,
            "object_name": "TestApp.debug.dylib",
        }

        in_memory_buffer = tempfile.NamedTemporaryFile(delete=False)
        with zipfile.ZipFile(in_memory_buffer, mode="w") as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest))
            zipf.writestr(f"files{file_path}", source_code)

        in_memory_buffer.seek(0)
        content = in_memory_buffer.read()
        checksum = sha1(content).hexdigest()

        fileblob = baker.make("files.FileBlob", checksum=checksum)
        in_memory_buffer.seek(0)
        fileblob.blob.save("source_bundle.zip", DjangoFile(in_memory_buffer))
        in_memory_buffer.close()

        file = baker.make("files.File", checksum=checksum, blob=fileblob)

        source_dif = baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
            name="TestApp.debug.dylib",
            data={
                "kind": "src",
                "debug_id": debug_id,
                "arch": "arm64",
                "symbol_type": "native",
                "features": ["sources"],
            },
        )

        return source_dif

    def test_ios_event_with_source_context(self):
        debug_id = "93ec5160-1d69-3227-8410-c2687fce4ea2"

        self.create_source_bundle_with_debug_symbols(debug_id)

        payload = {
            "platform": "cocoa",
            "contexts": {
                "device": {"arch": "arm64"},
                "os": {"name": "iOS", "version": "17.0"},
            },
            "exception": {
                "values": [
                    {
                        "type": "NSError",
                        "value": "Test error from iOS",
                        "stacktrace": {
                            "frames": [
                                {
                                    "function": "$s12ErrorFactory11ContentViewV07triggerA0yyF",
                                    "filename": "/Users/test/ContentView.swift",
                                    "lineno": 10,
                                    "in_app": True,
                                    "image_addr": "0x100000000",
                                    "instruction_addr": "0x100001000",
                                }
                            ]
                        },
                    }
                ]
            },
        }

        self.process_events(payload)

        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.data["platform"], "cocoa")

        exception = event.data["exception"]["values"][0]
        self.assertIn("stacktrace", exception)
        frames = exception["stacktrace"]["frames"]
        self.assertEqual(len(frames), 1)

    def test_ios_real_error_factory_payload(self):
        """Test with real payload captured from Error Factory iOS app"""
        payload = self.get_json_data("events/test_data/ios_error_factory.json")["data"]

        self.process_events(payload)

        event = IssueEvent.objects.first()
        self.assertIsNotNone(event)
        self.assertEqual(event.data["platform"], "cocoa")

        thread = event.data["threads"]["values"][0]
        self.assertIn("stacktrace", thread)
        frames = thread["stacktrace"]["frames"]
        self.assertEqual(len(frames), 61)

    def test_jvm_event_with_source_context(self):
        """
        Test that event_difs_resolve_stacktrace adds source context to JVM frames.
        Calls the DIF resolver directly to test the JVM source context wiring
        without depending on the pipeline's has_difs gating logic.
        """
        from apps.difs.tasks import event_difs_resolve_stacktrace

        debug_id = "b1c2d3e4-f5a6-7890-abcd-ef1234567890"

        # Create source bundle with Java source code
        source_code = (
            "package com.example;\n"
            "\n"
            "public class MyClass {\n"
            "    public void doSomething() {\n"
            '        throw new RuntimeException("test error");\n'
            "    }\n"
            "}\n"
        )
        # Use the real sentry-cli bundle path format: _/_/ prefix, .jvm extension
        file_path = "/_/_/com/example/MyClass.jvm"
        manifest = {
            "files": {
                f"files{file_path}": {
                    "type": "source",
                    "url": "~/com/example/MyClass.jvm",
                }
            },
            "debug_id": debug_id,
        }

        in_memory_buffer = tempfile.NamedTemporaryFile(delete=False)
        with zipfile.ZipFile(in_memory_buffer, mode="w") as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest))
            zipf.writestr(f"files{file_path}", source_code)

        in_memory_buffer.seek(0)
        content = in_memory_buffer.read()
        checksum = sha1(content).hexdigest()

        fileblob = baker.make("files.FileBlob", checksum=checksum)
        in_memory_buffer.seek(0)
        fileblob.blob.save("jvm_bundle.zip", DjangoFile(in_memory_buffer))
        in_memory_buffer.close()

        file = baker.make("files.File", checksum=checksum, blob=fileblob)
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
            data={"kind": "sources", "debug_id": debug_id},
        )

        payload = {
            "platform": "java",
            "timestamp": timezone.now().isoformat(),
            "event_id": uuid.uuid4().hex,
            "exception": {
                "values": [
                    {
                        "type": "RuntimeException",
                        "value": "test error",
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.MyClass",
                                    "filename": "MyClass.java",
                                    "function": "doSomething",
                                    "lineno": 5,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            },
            "debug_meta": {"images": [{"type": "jvm", "debug_id": debug_id}]},
        }
        event_schema = ErrorIssueEventSchema(**payload)

        # Call the DIF resolver directly
        event_difs_resolve_stacktrace(event_schema, self.project.id)

        # Verify source context was added to the frame
        frame = event_schema.exception.values[0].stacktrace.frames[0]
        self.assertEqual(
            frame.context_line,
            '        throw new RuntimeException("test error");',
        )
        self.assertIsNotNone(frame.pre_context)
        self.assertIsNotNone(frame.post_context)
        self.assertEqual(len(frame.pre_context), 4)

    def test_jvm_source_context_pipeline_no_release_env(self):
        """
        Test that JVM source context works through the full process_issue_events
        pipeline even when the event has NO release or environment.

        This is a regression test: previously, project_set was built only from
        events with release/environment, so projects from events missing both
        were never annotated with has_difs and DIF resolution was silently skipped.
        """
        debug_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

        # Create source bundle with Java source code
        source_code = (
            "package com.example;\n"
            "\n"
            "public class PipelineTest {\n"
            "    public void trigger() {\n"
            '        throw new RuntimeException("pipeline test");\n'
            "    }\n"
            "}\n"
        )
        file_path = "/_/_/com/example/PipelineTest.jvm"
        manifest = {
            "files": {
                f"files{file_path}": {
                    "type": "source",
                    "url": "~/com/example/PipelineTest.jvm",
                }
            },
            "debug_id": debug_id,
        }

        in_memory_buffer = tempfile.NamedTemporaryFile(delete=False)
        with zipfile.ZipFile(in_memory_buffer, mode="w") as zipf:
            zipf.writestr("manifest.json", json.dumps(manifest))
            zipf.writestr(f"files{file_path}", source_code)

        in_memory_buffer.seek(0)
        content = in_memory_buffer.read()
        checksum = sha1(content).hexdigest()

        fileblob = baker.make("files.FileBlob", checksum=checksum)
        in_memory_buffer.seek(0)
        fileblob.blob.save("jvm_pipeline_bundle.zip", DjangoFile(in_memory_buffer))
        in_memory_buffer.close()

        file = baker.make("files.File", checksum=checksum, blob=fileblob)
        baker.make(
            "difs.DebugInformationFile",
            project=self.project,
            file=file,
            data={"kind": "sources", "debug_id": debug_id},
        )

        payload = {
            "platform": "java",
            "timestamp": timezone.now().isoformat(),
            "event_id": uuid.uuid4().hex,
            # Intentionally NO release or environment
            "exception": {
                "values": [
                    {
                        "type": "RuntimeException",
                        "value": "pipeline test",
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "com.example.PipelineTest",
                                    "filename": "PipelineTest.java",
                                    "function": "trigger",
                                    "lineno": 5,
                                    "in_app": True,
                                }
                            ]
                        },
                    }
                ]
            },
            "debug_meta": {"images": [{"type": "jvm", "debug_id": debug_id}]},
        }

        # Go through the full pipeline with ErrorIssueEventSchema
        event = IssueTaskMessage(
            organization_id=self.organization.id,
            project_id=self.project.id,
            payload=ErrorIssueEventSchema(**payload),
            received=timezone.now(),
        )
        _process_issue_events([event])

        # Verify source context was persisted on the stored event
        issue_event = IssueEvent.objects.get_event(event.payload.event_id)
        self.assertIsNotNone(issue_event)
        frame = issue_event.data["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(
            frame["context_line"],
            '        throw new RuntimeException("pipeline test");',
        )
        self.assertIsNotNone(frame.get("pre_context"))
        self.assertIsNotNone(frame.get("post_context"))


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
        _process_issue_events([event])
        return IssueEvent.objects.get_event(event.payload.event_id)

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

        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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

        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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

        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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

        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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
        res = self.client.get(self.get_project_events_detail(event.id))
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

        res = self.client.get(self.get_project_events_detail(event.id))
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
        task_backends["default"].flush_batches()
        res_data = res.json()
        event = IssueEvent.objects.get_event(res_data["event_id"])
        event_json = self.get_event_json(event)
        self.assertCompareData(event_json, sentry_json, ["title", "extra", "user"])

        url = self.get_project_events_detail(event.id)
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

    def test_nul_bytes_stripped_from_transaction(self):
        """NUL bytes in transaction/culprit should be stripped before DB insert."""
        self.process_events([{"transaction": "foo\x00bar", "message": "nul test"}])
        event = IssueEvent.objects.first()
        self.assertNotIn("\x00", event.transaction)
        self.assertIn("foobar", event.transaction)

    def test_nul_bytes_stripped_from_tags(self):
        """NUL bytes in tag keys/values should be stripped before DB insert."""
        self.process_events(
            [{"tags": {"key\x00bad": "val\x00ue"}, "message": "nul tag test"}]
        )
        event = IssueEvent.objects.first()
        for key, value in event.tags.items():
            self.assertNotIn("\x00", key)
            self.assertNotIn("\x00", value)


class IssueEventIosContextTestCase(EventIngestTestCase):
    fileblobs = []

    def tearDown(self):
        for fileblob in self.fileblobs:
            fileblob.blob.delete()

    def test_ios_event_context(self):
        blobs_path = f"{COMPAT_TEST_DATA_DIR}/ios_event/uploads/file_blobs"

        filenames = [
            "82b270920467dac0c92e05e5fc06ece9bfe1499c",
            "4239f11a3846b08d5f3d1fa5229f2a316a86fcf0",
            "fd43cf8ad36ebbf3ad6f38474f916585a59a47b8",
        ]

        for filename in filenames:
            blob_path = os.path.join(blobs_path, filename)

            if not os.path.isfile(blob_path):
                assert False, f"Blob path {blob_path} does not exist or is not a file"

            with open(blob_path, "rb") as f:
                archive = Archive.open(blob_path)
                metadatalist = [
                    {
                        "arch": obj.arch,
                        "debug_id": normalize_debug_id(str(obj.debug_id)),
                        "kind": obj.kind,
                        "features": list(obj.features),
                        "symbol_type": "native",
                    }
                    for obj in archive.iter_objects()
                ]

                content = f.read()
                checksum = sha1(content).hexdigest()
                django_file = DjangoFile(f)
                fileblob = FileBlob.from_file(django_file)
                self.fileblobs.append(fileblob)

                file = baker.make("files.File", checksum=checksum, blob=fileblob)

                for metadata in metadatalist:
                    dif = baker.make(
                        "difs.DebugInformationFile",
                        project=self.project,
                        file=file,
                        name=filename,
                        data={
                            "arch": metadata["arch"],
                            "debug_id": metadata["debug_id"],
                            "kind": metadata["kind"],
                            "features": metadata["features"],
                            "symbol_type": metadata["symbol_type"],
                        },
                    )
                    dif.save()

        payload = self.get_json_data("events/test_data/ios_event/event.json")
        event_schema = ErrorIssueEventSchema(**payload)

        event_difs_resolve_stacktrace(event_schema, self.project.id)

        has_pre_context_and_post_context = False

        for frame in event_schema.exception.values[0].stacktrace.frames:
            if frame.pre_context and frame.post_context:
                has_pre_context_and_post_context = True
                break

        self.assertTrue(
            has_pre_context_and_post_context,
            "At least one frame should have both pre_context and post_context",
        )
