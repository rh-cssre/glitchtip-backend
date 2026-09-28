"""Characterization tests for what symbolication costs per event.

These record how many stored files symbolication opens, how many bytes it
reads, how many sourcemaps it parses, and which parser entry points it
calls. The numbers are what the code does now, not what it should do: each
test name says what it currently does, and its docstring says what the
intended behaviour is. A change that fixes one of these should flip the
assertion deliberately, and rename the test.
"""

import uuid

from django.utils import timezone

from apps.difs.models import DebugInformationFile

from ..process_event import process_issue_events
from ..schema import ErrorIssueEventSchema, IssueTaskMessage
from .symbolication import (
    APP_DEBUG_ID,
    ELF_DEBUG_ID,
    ELF_IMAGE_ADDR,
    IN_MEMORY_STORAGE,
    JS_FIXTURES,
    NATIVE_FIXTURES,
    PROGUARD_FIXTURES,
    crash_frame_offsets,
    debug_id_bundle,
    measure_symbolication,
    upload_artifact_bundle,
    upload_crash_sources,
    upload_native_dif,
    upload_native_project,
    upload_proguard_mapping,
)
from .utils import EventIngestTestCase, run_async_closing

# A project that has uploaded native debug files for a while: several
# builds, each uploaded more than once.
DISTINCT_DEBUG_IDS = 10
UPLOADS_PER_DEBUG_ID = 3
NATIVE_DEBUG_FILES = DISTINCT_DEBUG_IDS * UPLOADS_PER_DEBUG_ID

# gt_rust.symbolic entry points each path uses today.
SCHEMA_CALLS = {"normalize_debug_id", "parse_addr"}
NATIVE_CALLS = SCHEMA_CALLS | {
    "Archive.open",
    "Archive.get_object",
    "Archive.iter_objects",
    "SymCache.from_object",
    "SymCache.lookup",
    "demangle",
}
PROGUARD_CALLS = SCHEMA_CALLS | {"ProguardMapper.open", "ProguardMapper.remap_frame"}
JS_CALLS = SCHEMA_CALLS | {"SourceMapCache.from_bytes", "SourceMapCache.lookup"}


@IN_MEMORY_STORAGE
class SymbolicationCostTestCase(EventIngestTestCase):
    def process(self, payloads: list[dict]):
        messages = [
            IssueTaskMessage(
                project_id=self.project.id,
                organization_id=self.organization.id,
                received=timezone.now(),
                payload=ErrorIssueEventSchema(**payload),
            )
            for payload in payloads
        ]
        run_async_closing(process_issue_events, messages)


def base_event(**fields) -> dict:
    return {
        "event_id": uuid.uuid4().hex,
        "timestamp": timezone.now().isoformat(),
        "level": "error",
        **fields,
    }


def native_frames() -> list[dict]:
    return [
        {"instruction_addr": hex(ELF_IMAGE_ADDR + offset)}
        for offset in crash_frame_offsets("elf")
    ]


def native_crash(**fields) -> dict:
    return base_event(
        platform="native",
        exception={
            "values": [
                {
                    "type": "SIGSEGV",
                    "value": "Segfault",
                    "stacktrace": {"frames": native_frames()},
                }
            ]
        },
        **fields,
    )


class NativeDebugFileCostTests(SymbolicationCostTestCase):
    """A project with 30 native debug files, 3 uploads for each of 10 debug
    ids. One debug id is the real ELF fixture's."""

    def setUp(self):
        super().setUp()
        upload_native_project(self.project, DISTINCT_DEBUG_IDS, UPLOADS_PER_DEBUG_ID)
        self.total_bytes = sum(
            DebugInformationFile.objects.values_list("file__blob__size", flat=True)
        )

    def test_event_without_native_frames_reads_every_native_debug_file(self):
        """A plain exception with no instruction addresses cannot use a native
        debug file. Intended: no debug file is opened."""
        payload = base_event(
            platform="python",
            contexts={"os": {"name": "Linux"}, "runtime": {"name": "CPython"}},
            exception={
                "values": [
                    {
                        "type": "ValueError",
                        "value": "bad input",
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "app/views.py",
                                    "function": "handle",
                                    "lineno": 42,
                                }
                            ]
                        },
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.distinct_files_opened, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        self.assertEqual(cost.bytes_opened, self.total_bytes)
        self.assertEqual(len(cost.resolver_attempts), NATIVE_DEBUG_FILES)
        self.assertEqual(cost.archive_opens, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.symcache_builds, NATIVE_DEBUG_FILES)
        cost.assert_only(NATIVE_CALLS)

    def test_dart_exception_without_native_frames_reads_every_native_debug_file(
        self,
    ):
        """A Dart exception (Flutter on iOS) with only Dart frames. Intended:
        no debug file is opened."""
        payload = base_event(
            platform="dart",
            contexts={"os": {"name": "iOS"}, "device": {"arch": "arm64"}},
            exception={
                "values": [
                    {
                        "type": "StateError",
                        "value": "Bad state: no element",
                        "stacktrace": {
                            "frames": [
                                {
                                    "abs_path": "package:app/main.dart",
                                    "function": "main",
                                    "lineno": 12,
                                    "colno": 3,
                                }
                            ]
                        },
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        # The files are x86_64 and the event arm64: each is opened and read,
        # then no object in it matches.
        self.assertEqual(cost.archive_opens, NATIVE_DEBUG_FILES)
        cost.assert_only(NATIVE_CALLS)

    def test_native_frames_without_debug_meta_read_every_native_debug_file(self):
        """Native frames but no debug_meta, so no debug id to look up.
        Intended: a bounded number of candidates, newest first."""
        payload = native_crash(
            contexts={"os": {"name": "Android"}, "device": {"arch": "x86_64"}}
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        self.assertEqual(len(cost.resolver_attempts), NATIVE_DEBUG_FILES)
        cost.assert_only(NATIVE_CALLS)

    def test_debug_meta_reads_every_upload_of_its_debug_id(self):
        """With debug_meta, only files with the image's debug id are tried, but
        all of its uploads are. Intended: one file (or one cached lookup
        table) per debug id."""
        payload = native_crash(
            contexts={"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
            debug_meta={
                "images": [
                    {
                        "type": "elf",
                        "debug_id": ELF_DEBUG_ID,
                        "image_addr": hex(ELF_IMAGE_ADDR),
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, UPLOADS_PER_DEBUG_ID)
        self.assertEqual(cost.resolver_attempts, [ELF_DEBUG_ID] * UPLOADS_PER_DEBUG_ID)
        self.assertEqual(cost.archive_opens, UPLOADS_PER_DEBUG_ID)
        self.assertEqual(cost.symcache_builds, UPLOADS_PER_DEBUG_ID)
        cost.assert_only(NATIVE_CALLS)


class NativeSourceContextCostTests(SymbolicationCostTestCase):
    def test_source_bundle_is_reread_for_every_resolved_frame(self):
        """Source context re-opens and re-reads the whole source bundle for
        each frame it resolves. Intended: one read per bundle per event."""
        elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
        elf_dif = upload_native_dif(self.project, "crash.elf", elf, "lib", ELF_DEBUG_ID)
        bundle_dif = upload_crash_sources(self.project, ELF_DEBUG_ID)
        payload = native_crash(
            contexts={"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
            debug_meta={
                "images": [
                    {
                        "type": "elf",
                        "debug_id": ELF_DEBUG_ID,
                        "image_addr": hex(ELF_IMAGE_ADDR),
                    }
                ]
            },
        )
        frames = len(native_frames())
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(
            cost.opened,
            [elf_dif.file.blob.blob.name] + [bundle_dif.file.blob.blob.name] * frames,
        )
        self.assertEqual(cost.bytes_read, len(elf) + frames * bundle_dif.file.blob.size)
        cost.assert_only(NATIVE_CALLS)


class ProguardCostTests(SymbolicationCostTestCase):
    MAPPINGS = 20

    def setUp(self):
        super().setUp()
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        self.mapping_uuids = [str(uuid.uuid4()) for _ in range(self.MAPPINGS)]
        for index, mapping_uuid in enumerate(self.mapping_uuids):
            # Distinct content per upload, as each release's mapping would be.
            content = mapping + f"# build {index}\n".encode()
            upload_proguard_mapping(self.project, content, mapping_uuid)
        self.assertEqual(
            DebugInformationFile.objects.filter(data__symbol_type="proguard").count(),
            self.MAPPINGS,
        )
        self.total_bytes = sum(
            DebugInformationFile.objects.values_list("file__blob__size", flat=True)
        )

    def event(self) -> dict:
        # The event names one of the uploaded mappings.
        return base_event(
            platform="java",
            contexts={"os": {"name": "Android"}},
            debug_meta={
                "images": [{"type": "proguard", "uuid": self.mapping_uuids[7]}]
            },
            exception={
                "values": [
                    {
                        "type": "IllegalStateException",
                        "value": "boom",
                        "stacktrace": {
                            "frames": [
                                {"module": "a.a.a", "function": "b", "lineno": 2}
                            ]
                        },
                    }
                ]
            },
        )

    def select_mappings(self):
        # See ProguardSymbolicationGoldenTests.select_mapping.
        for dif in DebugInformationFile.objects.all():
            dif.data["kind"] = "mapping"
            dif.save(update_fields=["data"])

    def test_uploaded_mappings_are_never_selected(self):
        """Uploaded mappings have no ``kind`` in their data, and the DIF query's
        source-bundle exclusion drops such rows, so no mapping is ever read.
        Intended: exactly the mapping the event names is read (1 open)."""
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.proguard_opens, 0)
        self.assertEqual(cost.resolver_attempts, [])
        cost.assert_only(SCHEMA_CALLS)

        # Positive control: the same event, once the mappings are selected,
        # is seen reading them.
        self.select_mappings()
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertGreater(cost.files_opened, 0)
        self.assertGreater(cost.proguard_opens, 0)

    def test_selected_mappings_are_all_read(self):
        """Once mappings are selected, every mapping in the project is read and
        parsed for each event, whatever uuid the event names. Intended: only
        the matching mapping (1 open), or a bounded fallback without one."""
        self.select_mappings()
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertEqual(cost.files_opened, self.MAPPINGS)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        self.assertEqual(cost.proguard_opens, self.MAPPINGS)
        self.assertEqual(len(cost.resolver_attempts), self.MAPPINGS)
        cost.assert_only(PROGUARD_CALLS)


class JavascriptParseCostTests(SymbolicationCostTestCase):
    URL = "https://example.com/assets/app.min.js"

    def setUp(self):
        super().setUp()
        self.code = (JS_FIXTURES / "app.min.js").read_bytes()
        self.sourcemap = (JS_FIXTURES / "app.min.js.map").read_bytes()
        upload_artifact_bundle(
            self.organization,
            self.project,
            debug_id_bundle("app.min.js", self.code, self.sourcemap, APP_DEBUG_ID),
        )
        self.bundle_bytes = len(self.code) + len(self.sourcemap)

    def event(self) -> dict:
        frame = {"filename": self.URL, "function": "?", "lineno": 1}
        return base_event(
            platform="javascript",
            debug_meta={
                "images": [
                    {
                        "type": "sourcemap",
                        "code_file": self.URL,
                        "debug_id": APP_DEBUG_ID,
                    }
                ]
            },
            exception={
                "values": [
                    {
                        "type": "RangeError",
                        "value": "Cannot divide 3 by zero",
                        "stacktrace": {
                            "frames": [
                                {**frame, "colno": 345},
                                {**frame, "colno": 260},
                                {**frame, "colno": 147},
                            ]
                        },
                    }
                ]
            },
        )

    def test_frames_of_one_event_share_one_parse(self):
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertEqual(cost.sourcemap_parses, 1)
        self.assertEqual(cost.rust_calls["SourceMapCache.lookup"], 3)
        self.assertEqual(cost.files_opened, 2)
        self.assertEqual(cost.bytes_read, self.bundle_bytes)
        self.assertEqual(cost.bytes_opened, self.bundle_bytes)
        cost.assert_only(JS_CALLS)

    def test_every_event_reparses_the_bundle_across_batches(self):
        """Parsed sourcemaps live only as long as one event, so every event
        for the same bundle reads and parses it again. Intended: one parse per
        bundle, reused across events and batches."""
        events = 3
        with measure_symbolication() as cost:
            for _ in range(events):
                self.process([self.event()])
        self.assertEqual(cost.sourcemap_parses, events)
        self.assertEqual(cost.files_opened, 2 * events)
        self.assertEqual(cost.bytes_read, events * self.bundle_bytes)
        cost.assert_only(JS_CALLS)

    def test_every_event_reparses_the_bundle_within_a_batch(self):
        """The same holds inside one ingest batch. Intended: one parse."""
        events = 3
        with measure_symbolication() as cost:
            self.process([self.event() for _ in range(events)])
        self.assertEqual(cost.sourcemap_parses, events)
        self.assertEqual(cost.sourcemap_parse_bytes, events * self.bundle_bytes)
        cost.assert_only(JS_CALLS)
