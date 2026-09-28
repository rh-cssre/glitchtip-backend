"""Shared helpers for the symbolication parity and cost tests.

Three things live here:

* ``measure_symbolication``, a context manager that records what
  symbolication costs: which stored files it opens, how many bytes it reads
  from storage, how many sourcemaps it parses, and which debug files the
  native and ProGuard resolvers try.
* Upload helpers that store fixtures through the same code the upload APIs
  use, so the stored rows look like real uploads.
* ``make_large_bundle``, which generates a multi-megabyte minified bundle and
  sourcemap on the fly, since a fixture that size is not worth committing.

The fixtures themselves are in ``test_data/symbolication``; see
``generate.sh`` there.
"""

import contextlib
import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from apps.difs import stacktrace_processor
from apps.difs.api import create_dif_from_read_only_file
from apps.difs.models import DebugInformationFile
from apps.difs.tasks import difs_create_difs, difs_create_file_from_chunks
from apps.event_ingest import javascript_event_processor
from apps.files.assemble import assemble_artifacts
from apps.files.models import FileBlob

FIXTURES = Path(__file__).parent / "test_data" / "symbolication"
NATIVE_FIXTURES = FIXTURES / "native"
JS_FIXTURES = FIXTURES / "js"
PROGUARD_FIXTURES = FIXTURES / "proguard"
GOLDENS = FIXTURES / "goldens"

# Debug ids of the committed fixtures, as the upload path reads them.
APP_DEBUG_ID = "fa070d30-5516-9b7a-6571-c48a6f1e5bef"
WIDGET_DEBUG_ID = "8fd930b8-1625-09ac-11d3-f0bfc446a097"
ELF_DEBUG_ID = "95a3195e-fda2-0823-63b8-477b66ffaa4f"
MACHO_DEBUG_ID = "4c4c44c7-5555-3144-a125-2e9500c4f3e6"
PDB_DEBUG_ID = "2aa97044-953f-36ba-4c4c-44205044422e-1"
PROGUARD_UUID = "7a21010e-7dce-521b-893f-4973aca0ead9"
JVM_BUNDLE_DEBUG_ID = "0f6e8f8c-5c7b-4d59-9a1e-3c2b8d7e6f01"

# Where the fixture images are pretended to be loaded.
ELF_IMAGE_ADDR = 0x5555_5555_4000
MACHO_IMAGE_ADDR = 0x1_0000_0000
PE_IMAGE_ADDR = 0x1_4000_0000
# An address in no uploaded image, standing in for a system library frame.
FOREIGN_ADDR = 0x7FFF_F7A2_1B40


# --- Cost measurement --------------------------------------------------------


@dataclass
class SymbolicationCost:
    """What one measured block of symbolication did."""

    # Storage names of every file opened for reading, in order, repeats kept.
    opened: list[str] = field(default_factory=list)
    # Bytes read from storage across all of those opens.
    bytes_read: int = 0
    # SourceMapCache.from_bytes calls, and the bytes handed to them.
    sourcemap_parses: int = 0
    sourcemap_parse_bytes: int = 0
    # One entry per debug file the native/ProGuard resolver was run against:
    # the debug id recorded on that file.
    resolver_attempts: list[str | None] = field(default_factory=list)
    # Heavy native/ProGuard parser entry points.
    archive_opens: int = 0
    symcache_builds: int = 0
    proguard_opens: int = 0

    @property
    def files_opened(self) -> int:
        return len(self.opened)

    @property
    def distinct_files_opened(self) -> int:
        return len(set(self.opened))


class _CountingReader:
    """Wraps a storage file object and counts what is read through it."""

    def __init__(self, raw, cost: SymbolicationCost):
        self._raw = raw
        self._cost = cost

    def read(self, *args):
        data = self._raw.read(*args)
        self._cost.bytes_read += len(data)
        return data

    def readinto(self, buffer):
        count = self._raw.readinto(buffer)
        self._cost.bytes_read += count or 0
        return count

    def readline(self, *args):
        data = self._raw.readline(*args)
        self._cost.bytes_read += len(data)
        return data

    def __iter__(self):
        for line in self._raw:
            self._cost.bytes_read += len(line)
            yield line

    def __getattr__(self, name):
        return getattr(self._raw, name)


@contextlib.contextmanager
def measure_symbolication():
    """Record the storage reads and parser calls symbolication makes.

    Storage is measured at the storage backend's ``_open``, which every read of
    a stored blob goes through whatever the caller does with it afterwards, so
    the numbers stay comparable when the callers change. The parser counters
    patch the current entry points and are informational.
    """
    cost = SymbolicationCost()
    storage_class = type(storages["default"])
    original_open = storage_class._open

    def counting_open(storage, name, mode="rb"):
        django_file = original_open(storage, name, mode)
        cost.opened.append(name)
        django_file.file = _CountingReader(django_file.file, cost)
        return django_file

    sourcemap_from_bytes = javascript_event_processor.SourceMapCache.from_bytes

    def counting_sourcemap_from_bytes(minified, sourcemap):
        cost.sourcemap_parses += 1
        cost.sourcemap_parse_bytes += len(minified) + len(sourcemap)
        return sourcemap_from_bytes(minified, sourcemap)

    resolve_stacktrace = stacktrace_processor.StacktraceProcessor.resolve_stacktrace

    def counting_resolve_stacktrace(event, symbol_file, project_id=None, debug_id=None):
        cost.resolver_attempts.append(debug_id)
        return resolve_stacktrace(
            event, symbol_file, project_id=project_id, debug_id=debug_id
        )

    archive_open = stacktrace_processor.Archive.open
    symcache_from_object = stacktrace_processor.SymCache.from_object
    proguard_open = stacktrace_processor.ProguardMapper.open

    def counting(counter_name, func):
        def wrapper(*args, **kwargs):
            setattr(cost, counter_name, getattr(cost, counter_name) + 1)
            return func(*args, **kwargs)

        return wrapper

    with (
        mock.patch.object(storage_class, "_open", counting_open),
        mock.patch.object(
            javascript_event_processor.SourceMapCache,
            "from_bytes",
            counting_sourcemap_from_bytes,
        ),
        mock.patch.object(
            stacktrace_processor.StacktraceProcessor,
            "resolve_stacktrace",
            counting_resolve_stacktrace,
        ),
        mock.patch.object(
            stacktrace_processor.Archive,
            "open",
            counting("archive_opens", archive_open),
        ),
        mock.patch.object(
            stacktrace_processor.SymCache,
            "from_object",
            counting("symcache_builds", symcache_from_object),
        ),
        mock.patch.object(
            stacktrace_processor.ProguardMapper,
            "open",
            counting("proguard_opens", proguard_open),
        ),
    ):
        yield cost


# --- Uploads -----------------------------------------------------------------


def store_blob(name: str, content: bytes) -> FileBlob:
    """Store ``content`` as an uploaded chunk, keyed by its SHA-1."""
    checksum = hashlib.sha1(content).hexdigest()
    blob, created = FileBlob.objects.get_or_create(
        checksum=checksum, defaults={"size": len(content)}
    )
    if created:
        blob.blob.save(name, ContentFile(content))
    return blob


def upload_artifact_bundle(
    organization,
    project,
    artifacts: list[dict],
    release: str | None = None,
):
    """Upload a sourcemap artifact bundle the way the chunk-upload API does.

    Each artifact is ``{"name", "content", "type", "headers"}``; ``type`` is
    ``minified_source`` or ``source_map``. The bundle goes through
    ``assemble_artifacts``, which creates the File and DebugSymbolBundle rows.
    """
    manifest = {
        "files": {
            f"files/_/_/{artifact['name']}": {
                "type": artifact["type"],
                "url": f"~/{artifact['name']}",
                "headers": artifact.get("headers", {}),
            }
            for artifact in artifacts
        },
        "org": organization.slug,
        "project": project.slug,
    }
    if release:
        manifest["release"] = release
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for artifact in artifacts:
            zf.writestr(f"files/_/_/{artifact['name']}", artifact["content"])
    content = buffer.getvalue()
    blob = store_blob("artifact-bundle.zip", content)
    assemble_artifacts(organization, release, blob.checksum, [blob.checksum])


def debug_id_bundle(name: str, code: bytes, sourcemap: bytes, debug_id: str):
    """The artifact pair a debug-id aware SDK build tool uploads for a bundle."""
    return [
        {
            "name": name,
            "content": code,
            "type": "minified_source",
            "headers": {"debug-id": debug_id, "sourcemap": f"{name}.map"},
        },
        {
            "name": f"{name}.map",
            "content": sourcemap,
            "type": "source_map",
            "headers": {"debug-id": debug_id},
        },
    ]


def release_bundle(name: str, code: bytes, sourcemap: bytes):
    """The artifact pair a release-based (pre debug id) upload creates."""
    return [
        {
            "name": name,
            "content": code,
            "type": "minified_source",
            "headers": {"Sourcemap": f"{name}.map"},
        },
        {"name": f"{name}.map", "content": sourcemap, "type": "source_map"},
    ]


def upload_native_dif(project, name: str, content: bytes) -> list[DebugInformationFile]:
    """Upload a native debug file or source bundle the way ``difs_assemble`` does."""
    blob = store_blob(name, content)
    file = difs_create_file_from_chunks(name, blob.checksum, [blob.checksum])
    difs_create_difs(project, name, file)
    return list(DebugInformationFile.objects.filter(project=project, file=file))


def upload_proguard_mapping(project, content: bytes, mapping_uuid: str):
    """Upload a ProGuard mapping the way the dsyms upload API does."""
    return async_to_sync(create_dif_from_read_only_file)(
        io.BytesIO(content), project, mapping_uuid, f"proguard/{mapping_uuid}.txt"
    )


def make_source_bundle(debug_id: str, sources: dict[str, str], arch="any") -> bytes:
    """Build a source bundle in the format ``symbolic`` reads.

    That is an 8-byte header (``SYSB`` and a little-endian format version)
    followed by a zip of a manifest plus ``files/<path>`` entries. ``sources``
    maps the path recorded in the debug info (or the JVM bundle path) to the
    file's text. Entries carry a fixed timestamp so the bundle's bytes, and
    therefore its checksum, do not depend on when it was built.
    """
    manifest = {
        "files": {f"files{path}": {"type": "source", "path": path} for path in sources},
        "debug_id": debug_id,
        "arch": arch,
        "object_name": "fixture",
    }
    buffer = io.BytesIO()
    buffer.write(b"SYSB" + (2).to_bytes(4, "little"))
    with zipfile.ZipFile(buffer, mode="w") as zf:
        for entry_name, text in [("manifest.json", json.dumps(manifest))] + [
            (f"files{path}", text) for path, text in sources.items()
        ]:
            info = zipfile.ZipInfo(entry_name, date_time=(2020, 1, 1, 0, 0, 0))
            zf.writestr(info, text)
    return buffer.getvalue()


# --- Native fixture addresses ------------------------------------------------


def native_symbols() -> dict[str, dict[str, int]]:
    """Function offsets per object format, as written by the generator."""
    return json.loads((NATIVE_FIXTURES / "symbols.json").read_text())


# --- Large generated bundle --------------------------------------------------

_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _vlq(value: int) -> str:
    value = (-value << 1) | 1 if value < 0 else value << 1
    encoded = ""
    while True:
        digit = value & 31
        value >>= 5
        if value:
            digit |= 32
        encoded += _BASE64[digit]
        if not value:
            return encoded


@dataclass
class GeneratedBundle:
    code: bytes
    sourcemap: bytes
    debug_id: str
    # 1-based (lineno, colno) of the ``throw`` in each generated function,
    # keyed by (module index, function index): what a browser would report.
    throw_positions: dict[tuple[int, int], tuple[int, int]]


def make_large_bundle(modules: int = 160, functions: int = 100) -> GeneratedBundle:
    """Generate a minified bundle and sourcemap of a few megabytes.

    Each module holds ``functions`` small functions; the minified output puts
    all of them on one line, as minifiers do, and the sourcemap maps both the
    function and its ``throw`` back to the original source with names and
    sourcesContent. The output is deterministic.
    """
    sources = []
    sources_content = []
    names = []
    code_parts = []
    mappings = []
    throw_positions = {}
    column = 0
    previous = {"source": 0, "line": 0, "column": 0, "name": 0}

    def segment(generated_column, source, line, original_column, name=None):
        nonlocal column
        fields = [
            generated_column - column,
            source - previous["source"],
            line - previous["line"],
            original_column - previous["column"],
        ]
        column = generated_column
        previous.update(source=source, line=line, column=original_column)
        if name is not None:
            fields.append(name - previous["name"])
            previous["name"] = name
        mappings.append("".join(_vlq(value) for value in fields))

    offset = 0
    for module in range(modules):
        sources.append(f"webpack:///./src/generated/module_{module:03d}.js")
        lines = []
        for function in range(functions):
            original_name = f"computeTotal_{module}_{function}"
            names.append(original_name)
            name_index = len(names) - 1
            start_line = len(lines)
            lines += [
                f"export function {original_name}(firstNumber, secondNumber) {{",
                "  if (secondNumber < 0) {",
                f'    throw new Error("negative input in {original_name}");',
                "  }",
                "  return firstNumber + secondNumber;",
                "}",
            ]
            minified_name = f"m{module}_{function}"
            head = f"function {minified_name}(a,b){{if(b<0)"
            throw = f'throw new Error("negative input in {original_name}");'
            tail = "return a+b}"
            segment(offset, module, start_line, 0)
            segment(offset + len("function "), module, start_line, 16, name_index)
            throw_column = offset + len(head)
            segment(throw_column, module, start_line + 2, 4)
            throw_positions[(module, function)] = (1, throw_column + 1)
            part = head + throw + tail
            code_parts.append(part)
            offset += len(part)
        sources_content.append("\n".join(lines) + "\n")

    code = "".join(code_parts)
    debug_id = str(uuid.UUID(hashlib.sha1(code.encode()).hexdigest()[:32]))
    code += f"\n//# sourceMappingURL=large.min.js.map\n//# debugId={debug_id}\n"
    sourcemap = {
        "version": 3,
        "file": "large.min.js",
        "sources": sources,
        "sourcesContent": sources_content,
        "names": names,
        "mappings": ",".join(mappings),
        "debugId": debug_id,
    }
    return GeneratedBundle(
        code=code.encode(),
        sourcemap=json.dumps(sourcemap, separators=(",", ":")).encode(),
        debug_id=debug_id,
        throw_positions=throw_positions,
    )


# --- Goldens -----------------------------------------------------------------


def golden_view(issue_event) -> dict:
    """The parts of a stored event that symbolication decides."""
    return {
        "title": issue_event.issue.title,
        # The culprit is derived from the symbolicated frames and stored on
        # the event's transaction field.
        "culprit": issue_event.transaction,
        "metadata": issue_event.issue.metadata,
        "exception": issue_event.data.get("exception"),
    }
