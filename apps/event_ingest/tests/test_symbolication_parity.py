"""Golden tests that pin the symbolicated output of every symbolication path.

Each test uploads fixtures through the upload code, runs events through
``process_issue_events``, and compares what symbolication decides on the
stored event (frames, context, title, culprit) with a golden JSON file in
``test_data/symbolication/goldens``.

The goldens record current behaviour, quirks included, so a reimplementation
can be checked for identical results. When a change is meant to alter the
output, regenerate them and review the diff:

    UPDATE_SYMBOLICATION_GOLDENS=1 python manage.py test \\
        apps.event_ingest.tests.test_symbolication_parity
"""

import json
import os
import uuid

from django.utils import timezone

from apps.difs.models import DebugInformationFile
from apps.issue_events.models import IssueEvent

from ..process_event import process_issue_events
from ..schema import ErrorIssueEventSchema, IssueTaskMessage
from .symbolication import (
    APP_DEBUG_ID,
    ELF_DEBUG_ID,
    ELF_IMAGE_ADDR,
    FOREIGN_ADDR,
    GOLDENS,
    JS_FIXTURES,
    JVM_BUNDLE_DEBUG_ID,
    MACHO_DEBUG_ID,
    MACHO_IMAGE_ADDR,
    NATIVE_FIXTURES,
    PDB_DEBUG_ID,
    PE_IMAGE_ADDR,
    PROGUARD_FIXTURES,
    PROGUARD_UUID,
    WIDGET_DEBUG_ID,
    debug_id_bundle,
    golden_view,
    make_large_bundle,
    make_source_bundle,
    native_symbols,
    release_bundle,
    upload_artifact_bundle,
    upload_native_dif,
    upload_proguard_mapping,
)
from .utils import EventIngestTestCase, run_async_closing

UPDATE_GOLDENS = os.environ.get("UPDATE_SYMBOLICATION_GOLDENS") == "1"

EXISTING_TEST_DATA = JS_FIXTURES.parent.parent


def event_id(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"symbolication/{name}").hex


class SymbolicationGoldenTestCase(EventIngestTestCase):
    maxDiff = None

    def process(self, payloads: list[dict]) -> list[IssueEvent]:
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
        return [
            IssueEvent.objects.get_event(payload["event_id"]) for payload in payloads
        ]

    def assertMatchesGolden(self, name: str, events: list[IssueEvent]):
        actual = [golden_view(event) for event in events]
        path = GOLDENS / f"{name}.json"
        if UPDATE_GOLDENS:
            GOLDENS.mkdir(exist_ok=True)
            path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
            return
        self.assertTrue(
            path.exists(), f"No golden {path.name}; see the module docstring."
        )
        self.assertEqual(actual, json.loads(path.read_text()))


def js_frame(url: str, lineno: int | None, colno: int | None, function="?"):
    frame = {"filename": url, "function": function, "in_app": True}
    if lineno is not None:
        frame["lineno"] = lineno
    if colno is not None:
        frame["colno"] = colno
    return frame


def js_event(name: str, exceptions: list[dict], **extra) -> dict:
    return {
        "event_id": event_id(name),
        "timestamp": timezone.now().isoformat(),
        "platform": "javascript",
        "level": "error",
        "sdk": {"name": "sentry.javascript.browser", "version": "8.0.0"},
        "exception": {"values": exceptions},
        **extra,
    }


class JavascriptSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def read_js(self, name: str) -> tuple[bytes, bytes]:
        return (
            (JS_FIXTURES / name).read_bytes(),
            (JS_FIXTURES / f"{name}.map").read_bytes(),
        )

    def test_debug_id_bundles(self):
        """Debug-id bundles: two bundles, several frames sharing one, and
        frames with no map, no column, or an app:// URL."""
        app_code, app_map = self.read_js("app.min.js")
        widget_code, widget_map = self.read_js("widget.min.js")
        upload_artifact_bundle(
            self.organization,
            self.project,
            debug_id_bundle("app.min.js", app_code, app_map, APP_DEBUG_ID)
            + debug_id_bundle(
                "widget.min.js", widget_code, widget_map, WIDGET_DEBUG_ID
            ),
        )
        app_url = "https://example.com/assets/app.min.js"
        widget_url = "app:///assets/widget.min.js"
        payload = js_event(
            "js-debug-id",
            [
                {
                    "type": "Error",
                    "value": "renderWidget needs an element for large",
                    "stacktrace": {
                        "frames": [
                            js_frame(widget_url, 1, 188),
                            js_frame(widget_url, 1, 67),
                        ]
                    },
                },
                {
                    "type": "RangeError",
                    "value": "Cannot divide 3 by zero",
                    "stacktrace": {
                        "frames": [
                            js_frame("https://example.com/assets/vendor.js", 1, 10),
                            js_frame(app_url, 2, 75),
                            js_frame(app_url, 1, 372),
                            js_frame(app_url, 1, 345),
                            js_frame(app_url, 1, 260),
                            js_frame(app_url, 1, None),
                            js_frame(app_url, 1, 147),
                        ]
                    },
                    "mechanism": {"type": "onerror", "handled": False},
                },
            ],
            debug_meta={
                "images": [
                    {
                        "type": "sourcemap",
                        "code_file": app_url,
                        "debug_id": APP_DEBUG_ID,
                    },
                    {
                        "type": "sourcemap",
                        "code_file": widget_url,
                        "debug_id": WIDGET_DEBUG_ID,
                    },
                ]
            },
        )
        self.assertMatchesGolden("js_debug_id_bundles", self.process([payload]))

    def test_release_and_filename(self):
        """Release artifacts matched by file name: a webpack bundle whose
        sources carry webpack:// and node_modules paths."""
        code = (EXISTING_TEST_DATA / "bundle.js").read_bytes()
        sourcemap = (EXISTING_TEST_DATA / "bundle.js.map").read_bytes()
        upload_artifact_bundle(
            self.organization,
            self.project,
            release_bundle("bundle.js", code, sourcemap),
            release="web@1.0.0",
        )
        url = "http://localhost:8080/dist/bundle.js"
        payload = js_event(
            "js-release",
            [
                {
                    "type": "Error",
                    "value": "The error",
                    "stacktrace": {
                        "frames": [
                            js_frame(url, 2, 1799),
                            js_frame(url, 2, 73992),
                            js_frame(url, 2, 74012),
                            js_frame(url, 2, 74016),
                            js_frame("http://localhost:8080/dist/other.js", 1, 5),
                        ]
                    },
                    "mechanism": {"type": "onerror", "handled": False},
                }
            ],
            release="web@1.0.0",
        )
        self.assertMatchesGolden("js_release_and_filename", self.process([payload]))

    def test_no_matching_map(self):
        """Nothing resolves: a release with other files, and a debug-id
        minified file uploaded without its sourcemap."""
        code, sourcemap = self.read_js("widget.min.js")
        upload_artifact_bundle(
            self.organization,
            self.project,
            release_bundle("widget.min.js", code, sourcemap),
            release="web@2.0.0",
        )
        orphan_debug_id = "5d0c2f0e-8b0e-4a53-9d5b-2f5a3d4c1b10"
        app_code, _ = self.read_js("app.min.js")
        upload_artifact_bundle(
            self.organization,
            self.project,
            [
                {
                    "name": "orphan.min.js",
                    "content": app_code,
                    "type": "minified_source",
                    "headers": {"debug-id": orphan_debug_id},
                }
            ],
        )
        payload = js_event(
            "js-no-match",
            [
                {
                    "type": "TypeError",
                    "value": "x is undefined",
                    "stacktrace": {
                        "frames": [
                            js_frame("https://example.com/assets/main.js", 1, 50),
                            js_frame(
                                "https://example.com/assets/orphan.min.js", 1, 147
                            ),
                        ]
                    },
                }
            ],
            release="web@2.0.0",
            debug_meta={
                "images": [
                    {
                        "type": "sourcemap",
                        "code_file": "https://example.com/assets/orphan.min.js",
                        "debug_id": orphan_debug_id,
                    }
                ]
            },
        )
        self.assertMatchesGolden("js_no_matching_map", self.process([payload]))

    def test_large_bundle(self):
        """A generated bundle of a few megabytes, looked up at both ends and
        the middle."""
        bundle = make_large_bundle()
        upload_artifact_bundle(
            self.organization,
            self.project,
            debug_id_bundle(
                "large.min.js", bundle.code, bundle.sourcemap, bundle.debug_id
            ),
        )
        url = "https://example.com/assets/large.min.js"
        frames = [
            js_frame(url, *bundle.throw_positions[key])
            for key in [(0, 0), (80, 50), (159, 99)]
        ]
        payload = js_event(
            "js-large",
            [
                {
                    "type": "Error",
                    "value": "negative input",
                    "stacktrace": {"frames": frames},
                }
            ],
            debug_meta={
                "images": [
                    {
                        "type": "sourcemap",
                        "code_file": url,
                        "debug_id": bundle.debug_id,
                    }
                ]
            },
        )
        self.assertMatchesGolden("js_large_bundle", self.process([payload]))


def native_event(
    name: str,
    frames: list[dict],
    arch: str,
    os_name: str,
    images: list[dict] | None = None,
    platform="native",
) -> dict:
    payload = {
        "event_id": event_id(name),
        "timestamp": timezone.now().isoformat(),
        "platform": platform,
        "level": "fatal",
        "contexts": {"device": {"arch": arch}, "os": {"name": os_name}},
        "exception": {
            "values": [
                {
                    "type": "SIGSEGV",
                    "value": "Segfault",
                    "stacktrace": {"frames": frames},
                    "mechanism": {"type": "signalhandler", "handled": False},
                }
            ]
        },
    }
    if images is not None:
        payload["debug_meta"] = {"images": images}
    return payload


class NativeSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def setUp(self):
        super().setUp()
        self.symbols = native_symbols()

    def upload(self, filename: str, debug_id: str, with_sources=True):
        upload_native_dif(
            self.project, filename, (NATIVE_FIXTURES / filename).read_bytes()
        )
        if with_sources:
            source = (NATIVE_FIXTURES / "crash.cpp").read_text()
            upload_native_dif(
                self.project,
                f"{filename}.src.zip",
                make_source_bundle(debug_id, {"/src/native/crash.cpp": source}),
            )

    def frames(self, fmt: str, base: int, with_image_addr: bool) -> list[dict]:
        """The crash's frames, outermost first, plus one foreign frame."""
        # Return addresses land a few bytes into each caller.
        offsets = [
            ("main", 8),
            ("run_job", 20),
            ("crash_here", 24),
        ]
        frames = [
            {"instruction_addr": hex(FOREIGN_ADDR), "function": "__libc_start_main"}
        ]
        for function, delta in offsets:
            frame = {
                "instruction_addr": hex(base + self.symbols[fmt][function] + delta)
            }
            if with_image_addr:
                frame["image_addr"] = hex(base)
            frames.append(frame)
        return frames

    def test_elf_with_debug_meta(self):
        self.upload("crash.elf", ELF_DEBUG_ID)
        payload = native_event(
            "elf-debug-meta",
            self.frames("elf", ELF_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Linux",
            images=[
                {
                    "type": "elf",
                    "debug_id": ELF_DEBUG_ID,
                    "code_file": "/usr/bin/crash",
                    "image_addr": hex(ELF_IMAGE_ADDR),
                    "image_size": 0x4000,
                }
            ],
        )
        self.assertMatchesGolden("native_elf_debug_meta", self.process([payload]))

    def test_elf_without_debug_meta(self):
        """No debug_meta and no image_addr: the image base is estimated."""
        self.upload("crash.elf", ELF_DEBUG_ID)
        payload = native_event(
            "elf-no-debug-meta",
            self.frames("elf", ELF_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Linux",
        )
        self.assertMatchesGolden("native_elf_no_debug_meta", self.process([payload]))

    def test_elf_without_debug_meta_or_foreign_frames(self):
        """The Flutter-on-Android shape: only the app's own frames, absolute
        addresses, no image_addr and no debug_meta."""
        self.upload("crash.elf", ELF_DEBUG_ID)
        payload = native_event(
            "elf-no-debug-meta-own-frames",
            self.frames("elf", ELF_IMAGE_ADDR, with_image_addr=False)[1:],
            arch="x86_64",
            os_name="Android",
        )
        self.assertMatchesGolden(
            "native_elf_no_debug_meta_own_frames", self.process([payload])
        )

    def test_macho_with_debug_meta(self):
        """A dSYM, with image_addr on every frame as the Apple SDKs send."""
        self.upload("crash.macho.dwarf", MACHO_DEBUG_ID)
        payload = native_event(
            "macho-debug-meta",
            self.frames("macho", MACHO_IMAGE_ADDR, with_image_addr=True),
            arch="arm64",
            os_name="iOS",
            platform="cocoa",
            images=[
                {
                    "type": "macho",
                    "debug_id": MACHO_DEBUG_ID,
                    "code_file": "/private/var/containers/Crash.app/Crash",
                    "image_addr": hex(MACHO_IMAGE_ADDR),
                    "image_size": 0x8000,
                }
            ],
        )
        self.assertMatchesGolden("native_macho_debug_meta", self.process([payload]))

    def test_pdb_with_debug_meta(self):
        self.upload("crash.pdb", PDB_DEBUG_ID)
        payload = native_event(
            "pdb-debug-meta",
            self.frames("pe", PE_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Windows",
            images=[
                {
                    "type": "pe",
                    "debug_id": PDB_DEBUG_ID,
                    "code_file": "C:\\crash\\crash.exe",
                    "debug_file": "crash.pdb",
                    "image_addr": hex(PE_IMAGE_ADDR),
                    "image_size": 0x5000,
                }
            ],
        )
        self.assertMatchesGolden("native_pdb_debug_meta", self.process([payload]))

    def test_pdb_without_debug_meta(self):
        self.upload("crash.pdb", PDB_DEBUG_ID)
        payload = native_event(
            "pdb-no-debug-meta",
            self.frames("pe", PE_IMAGE_ADDR, with_image_addr=True),
            arch="x86_64",
            os_name="Windows",
        )
        self.assertMatchesGolden("native_pdb_no_debug_meta", self.process([payload]))


class ProguardSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def upload(self):
        upload_proguard_mapping(
            self.project,
            (PROGUARD_FIXTURES / "mapping.txt").read_bytes(),
            PROGUARD_UUID,
        )
        sources = {}
        for path in sorted((PROGUARD_FIXTURES / "src").rglob("*.kt")):
            relative = path.relative_to(PROGUARD_FIXTURES / "src").with_suffix(".jvm")
            sources[f"/_/_/{relative.as_posix()}"] = path.read_text()
        upload_native_dif(
            self.project,
            "jvm-sources.zip",
            make_source_bundle(JVM_BUNDLE_DEBUG_ID, sources),
        )

    def payload(self, name: str) -> dict:
        """An Android crash whose frames are obfuscated: an inlined call, a
        plain one, and a framework frame the mapping does not know."""
        return {
            "event_id": event_id(name),
            "timestamp": timezone.now().isoformat(),
            "platform": "java",
            "level": "error",
            "contexts": {"os": {"name": "Android", "version": "14"}},
            "exception": {
                "values": [
                    {
                        "type": "IllegalArgumentException",
                        "value": "value must be positive: -1",
                        "module": "java.lang",
                        "stacktrace": {
                            "frames": [
                                {
                                    "module": "android.view.View",
                                    "function": "performClick",
                                    "filename": "View.java",
                                    "lineno": 7659,
                                    "in_app": False,
                                },
                                {
                                    "module": "a.a.a",
                                    "function": "b",
                                    "filename": "SourceFile",
                                    "lineno": 2,
                                    "in_app": True,
                                },
                                {
                                    "module": "a.a.b",
                                    "function": "a",
                                    "filename": "SourceFile",
                                    "lineno": 3,
                                    "in_app": True,
                                },
                            ]
                        },
                    }
                ]
            },
            "debug_meta": {
                "images": [
                    {"type": "proguard", "uuid": PROGUARD_UUID},
                    {"type": "jvm", "debug_id": JVM_BUNDLE_DEBUG_ID},
                ]
            },
        }

    def test_uploaded_mapping(self):
        """A mapping and JVM source bundle as the upload API stores them.

        The DIF query excludes source bundles with ``exclude(data__kind__in=
        ...)``, which in SQL also drops every row whose data has no ``kind``
        key, and uploaded mappings have none. So the golden records the frames
        coming back still obfuscated.
        """
        self.upload()
        self.assertMatchesGolden(
            "proguard_uploaded_mapping", self.process([self.payload("proguard")])
        )

    def test_deobfuscation_and_source_context(self):
        """What the ProGuard resolver and JVM source context produce once the
        mapping is selected. The mapping is given a ``kind`` so the DIF query
        keeps it (see test_uploaded_mapping)."""
        self.upload()
        mapping = DebugInformationFile.objects.get(data__symbol_type="proguard")
        mapping.data["kind"] = "mapping"
        mapping.save(update_fields=["data"])
        self.assertMatchesGolden(
            "proguard_deobfuscation",
            self.process([self.payload("proguard-selected")]),
        )
