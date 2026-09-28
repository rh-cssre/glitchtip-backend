"""Characterization tests for what symbolication costs per event.

These record how many stored files symbolication opens, how many bytes it
reads, and how many sourcemaps it parses. The numbers are what the code does
now, not what it should do: each test name says what it currently does, and
its docstring says what the intended behaviour is. A change that fixes one of
these should flip the assertion deliberately, and rename the test.
"""

import uuid

from django.utils import timezone

from apps.difs.models import DebugInformationFile
from apps.difs.tasks import difs_create_file_from_chunks

from ..process_event import process_issue_events
from ..schema import ErrorIssueEventSchema, IssueTaskMessage
from .symbolication import (
    APP_DEBUG_ID,
    ELF_DEBUG_ID,
    ELF_IMAGE_ADDR,
    JS_FIXTURES,
    NATIVE_FIXTURES,
    PROGUARD_FIXTURES,
    debug_id_bundle,
    make_source_bundle,
    measure_symbolication,
    native_symbols,
    store_blob,
    upload_artifact_bundle,
    upload_native_dif,
    upload_proguard_mapping,
)
from .utils import EventIngestTestCase, run_async_closing

# The shape of a project that has uploaded native debug files for a while:
# many builds, each uploaded several times (CI retries, per-variant uploads).
DISTINCT_DEBUG_IDS = 50
UPLOADS_PER_DEBUG_ID = 4
NATIVE_DEBUG_FILES = DISTINCT_DEBUG_IDS * UPLOADS_PER_DEBUG_ID


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


class NativeDebugFileCostTests(SymbolicationCostTestCase):
    """A project with 200 native debug files, 4 uploads for each of 50 debug
    ids. One debug id is the real ELF fixture's."""

    def setUp(self):
        super().setUp()
        elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
        [real] = upload_native_dif(self.project, "crash.elf", elf)
        debug_ids = [ELF_DEBUG_ID] + [
            str(uuid.UUID(int=index + 1)) for index in range(DISTINCT_DEBUG_IDS - 1)
        ]
        for index in range(1, NATIVE_DEBUG_FILES):
            # Each re-upload is a distinct file: the same ELF with a different
            # tail, as a rebuild or a differently stripped copy would be.
            content = elf + index.to_bytes(4, "little")
            name = f"crash-{index:03d}.elf"
            blob = store_blob(name, content)
            file = difs_create_file_from_chunks(name, blob.checksum, [blob.checksum])
            DebugInformationFile.objects.create(
                project=self.project,
                name=name,
                file=file,
                data={
                    **real.data,
                    "debug_id": debug_ids[index // UPLOADS_PER_DEBUG_ID],
                },
            )
        self.total_bytes = sum(
            DebugInformationFile.objects.values_list("file__blob__size", flat=True)
        )
        self.elf_offsets = native_symbols()["elf"]

    def native_frames(self) -> list[dict]:
        return [
            {"instruction_addr": hex(ELF_IMAGE_ADDR + self.elf_offsets[name] + 8)}
            for name in ("main", "run_job", "crash_here")
        ]

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
        self.assertEqual(len(cost.resolver_attempts), NATIVE_DEBUG_FILES)
        self.assertEqual(cost.archive_opens, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.symcache_builds, NATIVE_DEBUG_FILES)

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

    def test_native_frames_without_debug_meta_read_every_native_debug_file(self):
        """Native frames but no debug_meta, so no debug id to look up.
        Intended: a bounded number of candidates, newest first."""
        payload = base_event(
            platform="native",
            contexts={"os": {"name": "Android"}, "device": {"arch": "x86_64"}},
            exception={
                "values": [
                    {
                        "type": "SIGSEGV",
                        "value": "Segfault",
                        "stacktrace": {"frames": self.native_frames()},
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, NATIVE_DEBUG_FILES)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        self.assertEqual(len(cost.resolver_attempts), NATIVE_DEBUG_FILES)

    def test_debug_meta_reads_every_upload_of_its_debug_id(self):
        """With debug_meta, only files with the image's debug id are tried, but
        all of its uploads are. Intended: one file (or one cached lookup
        table) per debug id."""
        payload = base_event(
            platform="native",
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
            exception={
                "values": [
                    {
                        "type": "SIGSEGV",
                        "value": "Segfault",
                        "stacktrace": {"frames": self.native_frames()},
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, UPLOADS_PER_DEBUG_ID)
        self.assertEqual(cost.resolver_attempts, [ELF_DEBUG_ID] * UPLOADS_PER_DEBUG_ID)
        self.assertEqual(cost.archive_opens, UPLOADS_PER_DEBUG_ID)


class NativeSourceContextCostTests(SymbolicationCostTestCase):
    def test_source_bundle_is_reread_for_every_resolved_frame(self):
        """Source context re-opens and re-reads the whole source bundle for
        each frame it resolves. Intended: one read per bundle per event."""
        elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
        [elf_dif] = upload_native_dif(self.project, "crash.elf", elf)
        source_bundle = make_source_bundle(
            ELF_DEBUG_ID,
            {"/src/native/crash.cpp": (NATIVE_FIXTURES / "crash.cpp").read_text()},
        )
        [bundle_dif] = upload_native_dif(self.project, "crash.src.zip", source_bundle)
        offsets = native_symbols()["elf"]
        frames = [
            {"instruction_addr": hex(ELF_IMAGE_ADDR + offsets[name] + 8)}
            for name in ("main", "run_job", "crash_here")
        ]
        payload = base_event(
            platform="native",
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
            exception={
                "values": [
                    {
                        "type": "SIGSEGV",
                        "value": "Segfault",
                        "stacktrace": {"frames": frames},
                    }
                ]
            },
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(
            cost.opened,
            [elf_dif.file.blob.blob.name]
            + [bundle_dif.file.blob.blob.name] * len(frames),
        )
        self.assertEqual(cost.bytes_read, len(elf) + len(frames) * len(source_bundle))


class ProguardCostTests(SymbolicationCostTestCase):
    MAPPINGS = 20

    def setUp(self):
        super().setUp()
        mapping = (PROGUARD_FIXTURES / "mapping.txt").read_bytes()
        for index in range(self.MAPPINGS):
            # Distinct content per upload, as each release's mapping would be.
            content = mapping + f"# build {index}\n".encode()
            upload_proguard_mapping(self.project, content, str(uuid.uuid4()))
        self.total_bytes = sum(
            DebugInformationFile.objects.values_list("file__blob__size", flat=True)
        )
        self.payload = base_event(
            platform="java",
            contexts={"os": {"name": "Android"}},
            debug_meta={"images": [{"type": "proguard", "uuid": str(uuid.uuid4())}]},
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

    def test_uploaded_mappings_are_never_selected(self):
        """Uploaded mappings have no ``kind`` in their data, and the DIF query's
        source-bundle exclusion drops such rows, so no mapping is ever read.
        Intended: the mapping matching the event's uuid is used."""
        with measure_symbolication() as cost:
            self.process([self.payload])
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.proguard_opens, 0)

    def test_selected_mappings_are_all_read(self):
        """Once mappings are selected, every mapping in the project is read and
        parsed for each event, whatever uuid the event names. Intended: only
        the matching mapping, or a bounded fallback."""
        for dif in DebugInformationFile.objects.all():
            dif.data["kind"] = "mapping"
            dif.save(update_fields=["data"])
        with measure_symbolication() as cost:
            self.process([self.payload])
        self.assertEqual(cost.files_opened, self.MAPPINGS)
        self.assertEqual(cost.bytes_read, self.total_bytes)
        self.assertEqual(cost.proguard_opens, self.MAPPINGS)


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
        self.assertEqual(cost.files_opened, 2)
        self.assertEqual(cost.bytes_read, len(self.code) + len(self.sourcemap))

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
        self.assertEqual(
            cost.bytes_read, events * (len(self.code) + len(self.sourcemap))
        )

    def test_every_event_reparses_the_bundle_within_a_batch(self):
        """The same holds inside one ingest batch. Intended: one parse."""
        events = 3
        with measure_symbolication() as cost:
            self.process([self.event() for _ in range(events)])
        self.assertEqual(cost.sourcemap_parses, events)
        self.assertEqual(
            cost.sourcemap_parse_bytes,
            events * (len(self.code) + len(self.sourcemap)),
        )
