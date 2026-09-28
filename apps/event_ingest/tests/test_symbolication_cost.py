"""What symbolication costs per event.

These pin how many stored files symbolication opens, how many bytes it
reads, how many caches it builds, and which parser entry points it calls.
Each artifact is built into a stored cache once, on first use; later events
load it from the in-process RAM tier without touching storage.
"""

import uuid

from django.test import override_settings
from django.utils import timezone

from apps.difs.models import DebugInformationFile
from apps.symbolication.models import CacheState, SymbolicationCache
from apps.symbolication.sources import COPY_CHUNK
from apps.symbolication.store import MAX_ATTEMPTS

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
    make_large_bundle,
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
FALLBACK_CANDIDATES = 3

# gt_rust.symbolic entry points each path may use.
# cache_format_tag runs once per cache kind per process.
SCHEMA_CALLS = {"normalize_debug_id", "parse_addr", "cache_format_tag"}
NATIVE_CALLS = SCHEMA_CALLS | {
    "SymCache.build_to",
    "SymCache.load",
    "SymCache.load_file",
    "SymCache.lookup",
    "demangle",
}
PROGUARD_CALLS = SCHEMA_CALLS | {
    "ProguardCache.build_to",
    "ProguardCache.load",
    "ProguardCache.load_file",
    "ProguardCache.remap_frame",
}
JS_CALLS = SCHEMA_CALLS | {
    "SourceMapCache.build_to",
    "SourceMapCache.load",
    "SourceMapCache.load_file",
    "SourceMapCache.lookup",
}


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

    def assertServedFromMemory(self, cost):
        """No build, no storage read, no parse: the RAM tier answered."""
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.bytes_read, 0)
        self.assertGreater(cost.cache_hits, 0)
        self.assertEqual(cost.cache_misses, 0)


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


def elf_debug_meta() -> dict:
    return {
        "images": [
            {
                "type": "elf",
                "debug_id": ELF_DEBUG_ID,
                "image_addr": hex(ELF_IMAGE_ADDR),
            }
        ]
    }


PYTHON_EXCEPTION = {
    "values": [
        {
            "type": "ValueError",
            "value": "bad input",
            "stacktrace": {
                "frames": [
                    {"filename": "app/views.py", "function": "handle", "lineno": 42}
                ]
            },
        }
    ]
}


@override_settings(GLITCHTIP_SYMBOLICATION_FALLBACK_CANDIDATES=FALLBACK_CANDIDATES)
class NativeDebugFileCostTests(SymbolicationCostTestCase):
    """A project with 30 native debug files, 3 uploads for each of 10 debug
    ids. One debug id is the real ELF fixture's."""

    def setUp(self):
        super().setUp()
        self.real = upload_native_project(
            self.project, DISTINCT_DEBUG_IDS, UPLOADS_PER_DEBUG_ID
        )
        self.elf_size = self.real.file.blob.size

    def test_event_without_native_frames_opens_no_debug_file(self):
        """A plain exception has no instruction addresses, so no native debug
        file can resolve it."""
        payload = base_event(
            platform="python",
            contexts={"os": {"name": "Linux"}, "runtime": {"name": "CPython"}},
            exception=PYTHON_EXCEPTION,
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.resolver_attempts, [])
        self.assertEqual(cost.cache_misses, 0)
        cost.assert_only(SCHEMA_CALLS)

        # Positive control: a native crash in the same project is seen
        # reading its debug file.
        with measure_symbolication() as cost:
            self.process([native_crash(debug_meta=elf_debug_meta())])
        self.assertGreater(cost.files_opened, 0)

    def test_dart_exception_without_native_frames_opens_no_debug_file(self):
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
        self.assertEqual(cost.files_opened, 0)
        self.assertEqual(cost.resolver_attempts, [])
        cost.assert_only(SCHEMA_CALLS)

        # Positive control, as above.
        with measure_symbolication() as cost:
            self.process([native_crash(debug_meta=elf_debug_meta())])
        self.assertGreater(cost.files_opened, 0)

    def test_native_frames_without_debug_meta_try_a_bounded_few(self):
        """No debug id to look up: the newest few distinct debug ids are
        tried, one file each, not every file in the project."""
        payload = native_crash(
            contexts={"os": {"name": "Android"}, "device": {"arch": "x86_64"}}
        )
        with measure_symbolication() as cost:
            self.process([payload])
        self.assertEqual(len(cost.resolver_attempts), FALLBACK_CANDIDATES)
        self.assertEqual(len(set(cost.resolver_attempts)), FALLBACK_CANDIDATES)
        self.assertEqual(cost.cache_builds, FALLBACK_CANDIDATES)
        # Each build reads its source once, and each cache is then loaded once.
        self.assertEqual(cost.files_opened, 2 * FALLBACK_CANDIDATES)
        cost.assert_only(NATIVE_CALLS)

    def test_debug_meta_builds_one_cache_for_its_debug_id(self):
        """With debug_meta, one of the debug id's uploads is used, however
        many there are: a miss is exactly one build."""
        with measure_symbolication() as cost:
            self.process([native_crash(debug_meta=elf_debug_meta())])
        self.assertEqual(cost.resolver_attempts, [ELF_DEBUG_ID])
        self.assertEqual(cost.cache_builds, 1)
        self.assertEqual(cost.cache_misses, 1)
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.files_written, 1)
        cost.assert_only(NATIVE_CALLS)

    def test_second_event_is_served_from_memory(self):
        self.process([native_crash(debug_meta=elf_debug_meta())])
        with measure_symbolication(cold=False) as cost:
            self.process([native_crash(debug_meta=elf_debug_meta())])
        self.assertServedFromMemory(cost)
        self.assertEqual(cost.resolver_attempts, [ELF_DEBUG_ID])
        cost.assert_only(
            NATIVE_CALLS - {"SymCache.build_to", "SymCache.load", "SymCache.load_file"}
        )


class NativeSourceContextCostTests(SymbolicationCostTestCase):
    def test_source_bundle_is_read_once_per_event(self):
        """Source context opens the source bundle once for all the frames it
        resolves, and reads its members in place."""
        elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
        upload_native_dif(self.project, "crash.elf", elf, "lib", ELF_DEBUG_ID)
        bundle_dif = upload_crash_sources(self.project, ELF_DEBUG_ID)
        payload = native_crash(
            contexts={"os": {"name": "Linux"}, "device": {"arch": "x86_64"}},
            debug_meta=elf_debug_meta(),
        )
        self.process([payload])
        with measure_symbolication(cold=False) as cost:
            self.process([payload | {"event_id": uuid.uuid4().hex}])
        self.assertEqual(cost.opened, [bundle_dif.file.blob.blob.name])
        self.assertEqual(cost.cache_builds, 0)
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

    def test_only_the_named_mapping_is_built(self):
        """Only the mapping the event names is used: one build, whatever else
        the project holds."""
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertEqual(cost.resolver_attempts, [self.mapping_uuids[7]])
        self.assertEqual(cost.cache_builds, 1)
        self.assertEqual(cost.proguard_opens, 0)  # no ProguardMapper at all
        cost.assert_only(PROGUARD_CALLS)

        with measure_symbolication(cold=False) as cost:
            self.process([self.event()])
        self.assertServedFromMemory(cost)


class ProguardSizeCapTests(SymbolicationCostTestCase):
    @override_settings(GLITCHTIP_PROGUARD_MAX_MAPPING_MB=0)
    def test_a_mapping_over_the_cap_is_never_built(self):
        """Over GLITCHTIP_PROGUARD_MAX_MAPPING_MB the build is refused before
        it allocates: the row is marked failed for good, and events are
        stored still obfuscated."""
        mapping_uuid = str(uuid.uuid4())
        upload_proguard_mapping(
            self.project, (PROGUARD_FIXTURES / "mapping.txt").read_bytes(), mapping_uuid
        )
        event = base_event(
            platform="java",
            contexts={"os": {"name": "Android"}},
            debug_meta={"images": [{"type": "proguard", "uuid": mapping_uuid}]},
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
        with measure_symbolication() as cost:
            self.process([event])
        row = SymbolicationCache.objects.get()
        self.assertEqual(row.state, CacheState.FAILED)
        self.assertEqual(row.attempts, MAX_ATTEMPTS)
        self.assertIn("cap", row.error)
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(cost.cache_unavailable, 1)

        with measure_symbolication() as cost:
            self.process([event | {"event_id": uuid.uuid4().hex}])
        self.assertEqual(cost.rust_calls["ProguardCache.build_to"], 0)
        self.assertEqual(cost.files_opened, 0)


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

    def event(self, url=URL, debug_id=APP_DEBUG_ID) -> dict:
        frame = {"filename": url, "function": "?", "lineno": 1}
        return base_event(
            platform="javascript",
            debug_meta={
                "images": [
                    {"type": "sourcemap", "code_file": url, "debug_id": debug_id}
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

    def test_a_miss_builds_the_cache_once(self):
        """The first event builds the bundle's cache, reading each source
        once; its frames share the loaded cache."""
        with measure_symbolication() as cost:
            self.process([self.event()])
        self.assertEqual(cost.cache_builds, 1)
        self.assertEqual(cost.sourcemap_parses, 0)  # no per-event from_bytes
        self.assertEqual(cost.rust_calls["SourceMapCache.build_to"], 1)
        self.assertEqual(cost.rust_calls["SourceMapCache.load_file"], 1)
        self.assertEqual(cost.rust_calls["SourceMapCache.lookup"], 3)
        cost.assert_only(JS_CALLS)

    def test_later_events_are_served_from_memory_across_batches(self):
        self.process([self.event()])
        with measure_symbolication(cold=False) as cost:
            for _ in range(3):
                self.process([self.event()])
        self.assertServedFromMemory(cost)
        self.assertEqual(cost.rust_calls["SourceMapCache.lookup"], 9)
        cost.assert_only(
            JS_CALLS
            - {
                "SourceMapCache.build_to",
                "SourceMapCache.load",
                "SourceMapCache.load_file",
            }
        )

    def test_events_in_one_batch_share_one_build(self):
        with measure_symbolication() as cost:
            self.process([self.event() for _ in range(3)])
        self.assertEqual(cost.cache_builds, 1)
        self.assertEqual(cost.rust_calls["SourceMapCache.load_file"], 1)

    def test_a_cold_process_loads_the_stored_cache_without_rebuilding(self):
        """A new process (empty RAM tier) loads the stored cache: one storage
        read, no build."""
        self.process([self.event()])
        with measure_symbolication(cold=True) as cost:
            self.process([self.event()])
        self.assertEqual(cost.cache_builds, 0)
        self.assertEqual(cost.cache_storage_loads, 1)
        self.assertEqual(cost.files_opened, 1)

    def test_large_sources_are_never_read_whole(self):
        """Building a multi-megabyte bundle streams its sources in chunks."""
        bundle = make_large_bundle()
        upload_artifact_bundle(
            self.organization,
            self.project,
            debug_id_bundle(
                "large.min.js", bundle.code, bundle.sourcemap, bundle.debug_id
            ),
        )
        url = "https://example.com/assets/large.min.js"
        with measure_symbolication() as cost:
            self.process([self.event(url=url, debug_id=bundle.debug_id)])
        self.assertEqual(cost.cache_builds, 1)
        self.assertGreater(len(bundle.sourcemap), COPY_CHUNK)
        # Sources stream in chunks; only the (smaller) cache is read whole.
        stored_cache = cost.bytes_opened - len(bundle.code) - len(bundle.sourcemap)
        self.assertLessEqual(cost.largest_read, max(COPY_CHUNK, stored_cache))
