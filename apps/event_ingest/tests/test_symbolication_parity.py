"""Golden tests that pin the symbolicated output of every symbolication path.

Each test uploads fixtures through the upload code, runs events through
``process_issue_events``, and compares what symbolication decides on the
stored event (frames, context, title, culprit, metadata) with a golden JSON
file in ``test_data/symbolication/goldens``.

The goldens record current behaviour, quirks and bugs included, so a
reimplementation can be checked for identical results. The values known to
be wrong are listed in ``KNOWN_WRONG`` below, with what they should be; a
fix changes those goldens on purpose. When a change is meant to alter the
output, regenerate the goldens and review the diff:

    docker compose run --rm -e UPDATE_SYMBOLICATION_GOLDENS=1 web \\
        python manage.py test apps.event_ingest.tests.test_symbolication_parity

In update mode each golden test writes its file and then skips; it refuses
to run when ``CI`` is set.
"""

import json
import os
import uuid
from typing import NamedTuple

from django.test import SimpleTestCase
from django.utils import timezone

from apps.difs.models import DebugInformationFile
from apps.files.models import File
from apps.issue_events.models import IssueEvent
from apps.shared.schema.exception import StackTraceFrame
from apps.sourcecode.models import DebugSymbolBundle

from ..javascript_event_processor import JavascriptEventProcessor
from ..process_event import process_issue_events
from ..schema import ErrorIssueEventSchema, IssueTaskMessage
from .symbolication import (
    APP_DEBUG_ID,
    ELF_DEBUG_ID,
    ELF_IMAGE_ADDR,
    FOREIGN_ADDR,
    GOLDENS,
    IN_MEMORY_STORAGE,
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
    crash_frame_offsets,
    debug_id_bundle,
    golden_view,
    make_large_bundle,
    make_source_bundle,
    release_bundle,
    upload_artifact_bundle,
    upload_crash_sources,
    upload_native_dif,
    upload_proguard_mapping,
)
from .utils import EventIngestTestCase, run_async_closing

UPDATE_GOLDENS = os.environ.get("UPDATE_SYMBOLICATION_GOLDENS") == "1"

EXISTING_TEST_DATA = JS_FIXTURES.parent.parent

# The frame URL of the app bundle carries a content hash, so its basename
# differs from the name the bundle was uploaded under: only the debug id
# (through debug_meta) can connect them.
APP_URL = "https://example.com/assets/app.3f9a1c.min.js"
# A different bundle uploaded under the hashed basename itself, with a debug
# id no event names. A lookup by file name alone would pick it.
DECOY_DEBUG_ID = "d3c0d3c0-0000-4000-8000-00000000d3c0"
WIDGET_NODE_MODULES_URL = "app:///node_modules/widget-kit/widget.min.js"
WIDGET_WEBPACK_URL = "webpack:///./dist/widget.min.js"


class KnownWrong(NamedTuple):
    """One value a golden records that is known to be wrong."""

    golden: str
    # Path to the value inside the golden: event index, then keys/indexes.
    path: tuple
    current: object
    expected: object
    why: str


def frame(event: int, exception: int, index: int, field: str) -> tuple:
    return (
        event,
        "exception",
        "values",
        exception,
        "stacktrace",
        "frames",
        index,
        field,
    )


# Values the goldens record that are questionable, and what they arguably
# should be. A change to one regenerates the golden and removes its entry
# here; test_known_wrong_entries_match_the_goldens keeps this list honest.
#
# Left on purpose: all are in_app decisions. in_app follows the SDK's value
# and the URL and source-path rules this processor was ported with, and it
# feeds grouping, so changing it is a behaviour change of its own, not a
# symbolication fix.
KNOWN_WRONG: list[KnownWrong] = [
    KnownWrong(
        "js_debug_id_bundles",
        frame(0, 0, 0, "in_app"),
        True,
        False,
        "an app:///node_modules/ URL comes out in_app: the app: branch decides "
        "from the original source path only",
    ),
    KnownWrong(
        "js_debug_id_bundles",
        frame(0, 1, 6, "in_app"),
        True,
        False,
        "a node_modules source keeps the SDK's in_app on an http(s) URL",
    ),
    KnownWrong(
        "js_release_and_filename",
        frame(0, 0, 0, "in_app"),
        True,
        False,
        "the tslib frame is a node_modules source but keeps the SDK's in_app",
    ),
]


def event_id(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"symbolication/{name}").hex


@IN_MEMORY_STORAGE
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
            if os.environ.get("CI"):
                self.fail("Refusing to update symbolication goldens in CI.")
            GOLDENS.mkdir(exist_ok=True)
            path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
            self.skipTest("golden updated")
        self.assertTrue(
            path.exists(), f"No golden {path.name}; see the module docstring."
        )
        self.assertEqual(actual, json.loads(path.read_text()))


def js_frame(url: str, lineno: int | None, colno: int | None, function="?", **extra):
    frame = {"filename": url, "function": function, "in_app": True, **extra}
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


def sourcemap_image(code_file: str, debug_id: str) -> dict:
    return {"type": "sourcemap", "code_file": code_file, "debug_id": debug_id}


def read_js(name: str) -> tuple[bytes, bytes]:
    return (
        (JS_FIXTURES / name).read_bytes(),
        (JS_FIXTURES / f"{name}.map").read_bytes(),
    )


class JavascriptSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def upload_debug_id_bundles(self):
        app_code, app_map = read_js("app.min.js")
        widget_code, widget_map = read_js("widget.min.js")
        upload_artifact_bundle(
            self.organization,
            self.project,
            debug_id_bundle("app.min.js", app_code, app_map, APP_DEBUG_ID)
            + debug_id_bundle("widget.min.js", widget_code, widget_map, WIDGET_DEBUG_ID)
            + debug_id_bundle(
                APP_URL.rsplit("/", 1)[-1], widget_code, widget_map, DECOY_DEBUG_ID
            ),
        )

    def test_debug_id_bundles(self):
        """Debug-id bundles: two bundles plus a decoy with the frame's
        basename, several frames sharing a bundle, frames with no map, no
        column, a minified function name, and in_app decided by the URL
        (omitted by the SDK on the app:// frame and the last app frame).

        Pins current bugs: an app:///node_modules/ frame comes out in_app, and a dependency frame
        the SDK marked in_app stays in_app. See KNOWN_WRONG. A webpack://
        frame URL is in test_webpack_frame_url.
        """
        self.upload_debug_id_bundles()
        payload = js_event(
            "js-debug-id",
            [
                {
                    "type": "Error",
                    "value": "renderWidget needs an element for large",
                    "stacktrace": {
                        "frames": [
                            js_frame(WIDGET_NODE_MODULES_URL, 1, 188, in_app=None),
                            js_frame(WIDGET_NODE_MODULES_URL, 1, 67),
                        ]
                    },
                },
                {
                    "type": "RangeError",
                    "value": "Cannot divide 3 by zero",
                    "stacktrace": {
                        "frames": [
                            js_frame("https://example.com/assets/vendor.js", 1, 10),
                            js_frame(APP_URL, 2, 75),
                            js_frame(APP_URL, 1, 372, function="t"),
                            js_frame(APP_URL, 1, 345),
                            js_frame(APP_URL, 1, 260),
                            js_frame(APP_URL, 1, None),
                            js_frame(APP_URL, 1, 37),
                            js_frame(APP_URL, 1, 147, in_app=None),
                        ]
                    },
                    "mechanism": {"type": "onerror", "handled": False},
                },
            ],
            debug_meta={
                "images": [
                    sourcemap_image(APP_URL, APP_DEBUG_ID),
                    sourcemap_image(WIDGET_NODE_MODULES_URL, WIDGET_DEBUG_ID),
                ]
            },
        )
        self.assertMatchesGolden("js_debug_id_bundles", self.process([payload]))

    def test_webpack_frame_url(self):
        """A frame whose URL is webpack:///..., in the same batch as an event
        with nothing to symbolicate: both are stored, and the frame's
        filename and module come from the webpack path."""
        self.upload_debug_id_bundles()
        plain = js_event(
            "js-webpack-neighbour",
            [{"type": "Error", "value": "unrelated", "stacktrace": {"frames": []}}],
        )
        webpack = js_event(
            "js-webpack",
            [
                {
                    "type": "Error",
                    "value": "renderWidget needs an element for large",
                    "stacktrace": {
                        "frames": [js_frame(WIDGET_WEBPACK_URL, 1, 67, in_app=None)]
                    },
                }
            ],
            debug_meta={
                "images": [sourcemap_image(WIDGET_WEBPACK_URL, WIDGET_DEBUG_ID)]
            },
        )
        self.assertMatchesGolden("js_webpack_frame_url", self.process([plain, webpack]))

    def test_two_events_in_one_batch(self):
        """Two events in one ingest batch naming different images: the first
        names the app bundle, the second only the widget, though its frame is
        in the app bundle. Only the first is symbolicated against it; the
        second would be alone in a batch too."""
        self.upload_debug_id_bundles()
        exception = {
            "type": "RangeError",
            "value": "Cannot divide 3 by zero",
            "stacktrace": {"frames": [js_frame(APP_URL, 1, 260)]},
        }
        first = js_event(
            "js-batch-first",
            [exception],
            debug_meta={"images": [sourcemap_image(APP_URL, APP_DEBUG_ID)]},
        )
        second = js_event(
            "js-batch-second",
            [exception],
            debug_meta={
                "images": [sourcemap_image(WIDGET_NODE_MODULES_URL, WIDGET_DEBUG_ID)]
            },
        )
        self.assertMatchesGolden(
            "js_two_events_one_batch", self.process([first, second])
        )

    def test_release_and_filename(self):
        """Release artifacts matched by file name: a webpack bundle whose
        sources carry webpack:// and node_modules paths.

        Pins a known limitation: the tslib frame (a node_modules source)
        keeps the SDK's in_app. See KNOWN_WRONG.
        """
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
        code, sourcemap = read_js("widget.min.js")
        upload_artifact_bundle(
            self.organization,
            self.project,
            release_bundle("widget.min.js", code, sourcemap),
            release="web@2.0.0",
        )
        orphan_debug_id = "5d0c2f0e-8b0e-4a53-9d5b-2f5a3d4c1b10"
        app_code, _ = read_js("app.min.js")
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
        orphan_url = "https://example.com/assets/orphan.min.js"
        payload = js_event(
            "js-no-match",
            [
                {
                    "type": "TypeError",
                    "value": "x is undefined",
                    "stacktrace": {
                        "frames": [
                            js_frame("https://example.com/assets/main.js", 1, 50),
                            js_frame(orphan_url, 1, 147),
                        ]
                    },
                }
            ],
            release="web@2.0.0",
            debug_meta={"images": [sourcemap_image(orphan_url, orphan_debug_id)]},
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
            debug_meta={"images": [sourcemap_image(url, bundle.debug_id)]},
        )
        self.assertMatchesGolden("js_large_bundle", self.process([payload]))


class JavascriptBundleSelectionTests(SimpleTestCase):
    """Which uploaded bundle a frame is matched to, at the processor level.

    The goldens cannot tell a debug-id match from a file-name match: the
    ingest pipeline copies the debug_meta code_file onto the matching bundle
    first, so both routes find the same bundle. These call
    ``find_source_files`` directly with bundles in a fixed order.
    """

    def bundles(self, decoy_first: bool):
        app = DebugSymbolBundle(
            debug_id=APP_DEBUG_ID,
            file=File(name="app.min.js"),
            sourcemap_file=File(name="app.min.js.map"),
            data={},
        )
        decoy = DebugSymbolBundle(
            debug_id=DECOY_DEBUG_ID,
            file=File(name=APP_URL.rsplit("/", 1)[-1]),
            sourcemap_file=File(name="decoy.js.map"),
            data={},
        )
        return app, decoy, [decoy, app] if decoy_first else [app, decoy]

    def find(self, bundles):
        processor = JavascriptEventProcessor(None, None, bundles)
        frame = StackTraceFrame(abs_path=APP_URL, lineno=1, colno=1)
        debug_id_map = {APP_URL.rsplit("/", 1)[-1]: APP_DEBUG_ID}
        return processor.find_source_files(frame, debug_id_map)

    def test_debug_id_match_beats_a_later_file_name_match(self):
        app, _, bundles = self.bundles(decoy_first=False)
        self.assertEqual(self.find(bundles), (app.file, app.sourcemap_file))

    def test_debug_id_match_beats_an_earlier_file_name_match(self):
        app, _, bundles = self.bundles(decoy_first=True)
        self.assertEqual(self.find(bundles), (app.file, app.sourcemap_file))


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


def crash_frames(fmt: str, base: int, with_image_addr: bool) -> list[dict]:
    """The crash's frames, outermost first, after one system library frame.
    Addresses are the real return addresses and faulting instruction."""
    frames = [{"instruction_addr": hex(FOREIGN_ADDR), "function": "__libc_start_main"}]
    for offset in crash_frame_offsets(fmt):
        frame = {"instruction_addr": hex(base + offset)}
        if with_image_addr:
            frame["image_addr"] = hex(base)
        frames.append(frame)
    return frames


class NativeSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def upload(self, filename: str, kind: str, debug_id: str):
        upload_native_dif(
            self.project,
            filename,
            (NATIVE_FIXTURES / filename).read_bytes(),
            kind,
            debug_id,
        )
        upload_crash_sources(self.project, debug_id)

    def elf_image(self):
        return {
            "type": "elf",
            "debug_id": ELF_DEBUG_ID,
            "code_file": "/usr/bin/crash",
            "image_addr": hex(ELF_IMAGE_ADDR),
            "image_size": 0x4000,
        }

    def test_elf_with_debug_meta(self):
        self.upload("crash.elf", "lib", ELF_DEBUG_ID)
        payload = native_event(
            "elf-debug-meta",
            crash_frames("elf", ELF_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Linux",
            images=[self.elf_image()],
        )
        self.assertMatchesGolden("native_elf_debug_meta", self.process([payload]))

    def test_elf_with_addresses_already_adjusted(self):
        """sentry-native built with libunwindstack sends frames already moved
        back from their return addresses, and says so with
        instruction_addr_adjustment "none". They are looked up as sent: no
        second adjustment."""
        self.upload("crash.elf", "lib", ELF_DEBUG_ID)
        frames = crash_frames("elf", ELF_IMAGE_ADDR, with_image_addr=False)
        for frame in frames[1:-1]:  # the callers, already adjusted
            frame["instruction_addr"] = hex(int(frame["instruction_addr"], 16) - 1)
        payload = native_event(
            "elf-adjustment-none",
            frames,
            arch="x86_64",
            os_name="Android",
            images=[self.elf_image()],
        )
        payload["exception"]["values"][0]["stacktrace"][
            "instruction_addr_adjustment"
        ] = "none"
        self.assertMatchesGolden("native_elf_adjustment_none", self.process([payload]))

    def test_elf_with_and_without_debug_info(self):
        """Two uploads for one debug id, the full ELF and a copy with its
        DWARF stripped (uploaded later, so tried first). The result with
        source context is kept."""
        self.upload("crash.elf", "lib", ELF_DEBUG_ID)
        upload_native_dif(
            self.project,
            "crash.stripped.elf",
            (NATIVE_FIXTURES / "crash.stripped.elf").read_bytes(),
            "lib",
            ELF_DEBUG_ID,
        )
        payload = native_event(
            "elf-debug-and-stripped",
            crash_frames("elf", ELF_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Linux",
            images=[self.elf_image()],
        )
        self.assertMatchesGolden(
            "native_elf_debug_and_stripped", self.process([payload])
        )

    def test_elf_without_debug_meta(self):
        """No debug_meta and no image_addr: the image base is estimated, even
        though the first frame is in another image (a system library)."""
        self.upload("crash.elf", "lib", ELF_DEBUG_ID)
        payload = native_event(
            "elf-no-debug-meta",
            crash_frames("elf", ELF_IMAGE_ADDR, with_image_addr=False),
            arch="x86_64",
            os_name="Linux",
        )
        self.assertMatchesGolden("native_elf_no_debug_meta", self.process([payload]))

    def test_elf_without_debug_meta_or_foreign_frames(self):
        """The Flutter-on-Android shape: only the app's own frames, absolute
        addresses, no image_addr and no debug_meta."""
        self.upload("crash.elf", "lib", ELF_DEBUG_ID)
        payload = native_event(
            "elf-no-debug-meta-own-frames",
            crash_frames("elf", ELF_IMAGE_ADDR, with_image_addr=False)[1:],
            arch="x86_64",
            os_name="Android",
        )
        self.assertMatchesGolden(
            "native_elf_no_debug_meta_own_frames", self.process([payload])
        )

    def test_macho_with_debug_meta(self):
        """A dSYM, with image_addr on every frame as the Apple SDKs send."""
        self.upload("crash.macho.dwarf", "dbg", MACHO_DEBUG_ID)
        payload = native_event(
            "macho-debug-meta",
            crash_frames("macho", MACHO_IMAGE_ADDR, with_image_addr=True),
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
        """The PDB is found by its debug id, age included."""
        self.upload("crash.pdb", "dbg", PDB_DEBUG_ID)
        payload = native_event(
            "pdb-debug-meta",
            crash_frames("pe", PE_IMAGE_ADDR, with_image_addr=False),
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
        self.upload("crash.pdb", "dbg", PDB_DEBUG_ID)
        payload = native_event(
            "pdb-no-debug-meta",
            crash_frames("pe", PE_IMAGE_ADDR, with_image_addr=True),
            arch="x86_64",
            os_name="Windows",
        )
        self.assertMatchesGolden("native_pdb_no_debug_meta", self.process([payload]))


def proguard_frames() -> list[dict]:
    """Obfuscated frames: a framework frame the mapping does not know, a
    plain call, and an inlined one."""
    return [
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


def proguard_event(name: str, exceptions: list[dict]) -> dict:
    return {
        "event_id": event_id(name),
        "timestamp": timezone.now().isoformat(),
        "platform": "java",
        "level": "error",
        "contexts": {"os": {"name": "Android", "version": "14"}},
        "exception": {"values": exceptions},
        "debug_meta": {
            "images": [
                {"type": "proguard", "uuid": PROGUARD_UUID},
                {"type": "jvm", "debug_id": JVM_BUNDLE_DEBUG_ID},
            ]
        },
    }


ILLEGAL_ARGUMENT = {
    "type": "IllegalArgumentException",
    "value": "value must be positive: -1",
    "module": "java.lang",
    "stacktrace": {"frames": proguard_frames()},
}


class ProguardSymbolicationGoldenTests(SymbolicationGoldenTestCase):
    def upload(self):
        upload_proguard_mapping(
            self.project,
            (PROGUARD_FIXTURES / "mapping.txt").read_bytes(),
            PROGUARD_UUID,
        )
        self.assertTrue(
            DebugInformationFile.objects.filter(
                project=self.project,
                data__debug_id=PROGUARD_UUID,
                data__symbol_type="proguard",
            ).exists()
        )
        sources = {}
        for path in sorted((PROGUARD_FIXTURES / "src").rglob("*.kt")):
            relative = path.relative_to(PROGUARD_FIXTURES / "src").with_suffix(".jvm")
            sources[f"/_/_/{relative.as_posix()}"] = path.read_text()
        upload_native_dif(
            self.project,
            "jvm-sources.zip",
            make_source_bundle(JVM_BUNDLE_DEBUG_ID, sources),
            "src",
            JVM_BUNDLE_DEBUG_ID,
        )

    def test_deobfuscation_and_source_context(self):
        """A mapping and JVM source bundle as the upload API stores them.

        The inlined call (Validator.requirePositive inside
        UserRepository.loadUser) comes back as both frames.
        """
        self.upload()
        self.assertMatchesGolden(
            "proguard_deobfuscation",
            self.process([proguard_event("proguard", [ILLEGAL_ARGUMENT])]),
        )

    def test_chained_exceptions(self):
        """A wrapped exception and its cause, both obfuscated: both are
        deobfuscated."""
        self.upload()
        wrapper = {
            "type": "RuntimeException",
            "value": "click handler failed",
            "module": "java.lang",
            "stacktrace": {"frames": proguard_frames()[:2]},
        }
        self.assertMatchesGolden(
            "proguard_chained_exceptions",
            self.process(
                [proguard_event("proguard-chained", [ILLEGAL_ARGUMENT, wrapper])]
            ),
        )


class KnownWrongTests(SimpleTestCase):
    def test_known_wrong_entries_match_the_goldens(self):
        """Every KNOWN_WRONG entry still describes its golden. When a fix
        regenerates a golden, this fails until the entry is removed."""
        for entry in KNOWN_WRONG:
            with self.subTest(golden=entry.golden, path=entry.path):
                value = json.loads((GOLDENS / f"{entry.golden}.json").read_text())
                for step in entry.path:
                    if isinstance(value, dict):
                        value = value.get(step)
                    else:
                        value = value[step]
                self.assertEqual(value, entry.current)
                self.assertNotEqual(entry.current, entry.expected)
