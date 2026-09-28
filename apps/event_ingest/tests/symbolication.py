"""Shared helpers for the symbolication parity and cost tests.

* ``measure_symbolication``, a context manager that records what
  symbolication costs: which stored files it opens (at ``Storage.open``, and
  reopens of an open ``FieldFile``), how many bytes it reads and writes,
  every call into ``gt_rust.symbolic``, which debug files the native and
  ProGuard resolvers try, and the symbolication cache's hits, misses, loads
  and builds. ``cold=True`` (the default) first empties the process caches
  registered in ``CACHE_RESET_HOOKS``.
* ``IN_MEMORY_STORAGE``, which the tests apply so fixtures never touch the
  repository's uploads/ directory.
* Upload helpers that store fixtures through the same code the upload APIs
  use and check that the upload created what a real one would.
* ``make_large_bundle``, which generates a multi-megabyte minified bundle and
  sourcemap on the fly, since a fixture that size is not worth committing.

The fixtures themselves are in ``test_data/symbolication``; see
``generate.sh`` there.
"""

import contextlib
import hashlib
import inspect
import io
import json
import sys
import threading
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.base import File as DjangoFile
from django.core.files.storage import Storage
from django.db.models.fields.files import FieldFile
from django.test import override_settings
from gt_rust import symbolic

from apps.difs.api import create_dif_from_read_only_file
from apps.difs.models import DebugInformationFile
from apps.difs.stacktrace_processor import StacktraceProcessor
from apps.difs.tasks import difs_create_difs, difs_create_file_from_chunks
from apps.files.assemble import assemble_artifacts
from apps.files.models import FileBlob
from apps.sourcecode.models import DebugSymbolBundle
from apps.symbolication import store as symbolication_store

FIXTURES = Path(__file__).parent / "test_data" / "symbolication"
NATIVE_FIXTURES = FIXTURES / "native"
# The source the native fixtures are built from, as compiled.
CRASH_SOURCE = FIXTURES / "generator" / "native" / "crash.cpp"
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

# Tests that measure symbolication, or store fixtures for it, run against an
# in-memory default storage: nothing lands in the repository's uploads/
# directory, and every read is visible to the instruments below.
IN_MEMORY_STORAGE = override_settings(
    STORAGES={
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    }
)

# Called by ``measure_symbolication(cold=True)`` before measuring, so a run
# starts with every symbolication cache empty. A cache layer adds a function
# here that empties it; with none registered, cold and warm are the same.
CACHE_RESET_HOOKS: list = [symbolication_store.clear_process_caches]


@dataclass
class SymbolicationCost:
    """What one measured block of symbolication did."""

    # Storage names of every file opened for reading, in order, repeats kept.
    # A reopen of an already-open stored file counts as an open.
    opened: list[str] = field(default_factory=list)
    # Opens per storage class, e.g. {"InMemoryStorage": 3}.
    opened_by_storage: Counter = field(default_factory=Counter)
    # Names of stored files reopened through an existing FieldFile.
    reopens: list[str] = field(default_factory=list)
    # Bytes actually read through those opens, and the largest single read.
    bytes_read: int = 0
    largest_read: int = 0
    # Stored size of every file at open time: an upper bound on bytes_read
    # that does not depend on how the caller reads.
    bytes_opened: int = 0
    # Files written to storage while measuring, and their bytes.
    files_written: int = 0
    bytes_written: int = 0
    # Calls into gt_rust.symbolic by qualified name, e.g.
    # "SourceMapCache.from_bytes", and the bytes passed in as arguments.
    rust_calls: Counter = field(default_factory=Counter)
    rust_bytes: Counter = field(default_factory=Counter)
    # One entry per debug file the native/ProGuard resolver was run against:
    # the debug id recorded on that file.
    resolver_attempts: list[str | None] = field(default_factory=list)
    # Symbolication cache events (see apps.symbolication.store): RAM tier
    # hits and misses, loads from the disk tier and from file storage, cache
    # builds, and lookups that went on without a cache.
    cache_hits: int = 0
    cache_misses: int = 0
    cache_disk_hits: int = 0
    # A stored cache read into memory, memory-mapped in place (local file
    # storage), or shared from another event's load already in flight.
    cache_storage_loads: int = 0
    cache_storage_maps: int = 0
    cache_shared_loads: int = 0
    cache_builds: int = 0
    cache_unavailable: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, **increments):
        """Add to counters; safe from the worker threads ingest runs on."""
        with self._lock:
            for name, amount in increments.items():
                setattr(self, name, getattr(self, name) + amount)

    @property
    def files_opened(self) -> int:
        return len(self.opened)

    @property
    def distinct_files_opened(self) -> int:
        return len(set(self.opened))

    @property
    def sourcemap_parses(self) -> int:
        return self.rust_calls["SourceMapCache.from_bytes"]

    @property
    def sourcemap_parse_bytes(self) -> int:
        return self.rust_bytes["SourceMapCache.from_bytes"]

    @property
    def archive_opens(self) -> int:
        return self.rust_calls["Archive.open"] + self.rust_calls["Archive.from_bytes"]

    @property
    def symcache_builds(self) -> int:
        return (
            self.rust_calls["SymCache.from_object"]
            + self.rust_calls["SymCache.from_bytes"]
            + self.rust_calls["Object.make_symcache"]
        )

    @property
    def proguard_opens(self) -> int:
        return (
            self.rust_calls["ProguardMapper.open"]
            + self.rust_calls["ProguardMapper.from_bytes"]
        )

    def assert_only(self, allowed: set[str]):
        """Fail if any gt_rust.symbolic entry point outside ``allowed`` ran.

        Pins the set of parser entry points a scenario uses, so a new one
        cannot do work the named counters do not see.
        """
        unexpected = set(self.rust_calls) - set(allowed)
        if unexpected:
            raise AssertionError(
                f"unexpected gt_rust.symbolic calls: {sorted(unexpected)} "
                f"(all calls: {dict(self.rust_calls)})"
            )


class _CountingReader:
    """Wraps a stored file's stream and counts what is read through it.

    Only the attributes below are available. Anything else that could read
    around the count (fileno, getvalue, getbuffer, ...) raises, so a new
    read path shows up as an error rather than as zero bytes.
    """

    _PASSTHROUGH = frozenset(
        {
            "seek",
            "tell",
            "close",
            "closed",
            "name",
            "mode",
            "size",
            "seekable",
            "readable",
        }
    )

    def __init__(self, raw, cost: SymbolicationCost):
        self._raw = raw
        self._cost = cost

    def read(self, *args):
        data = self._raw.read(*args)
        self._cost.record(bytes_read=len(data))
        with self._cost._lock:
            self._cost.largest_read = max(self._cost.largest_read, len(data))
        return data

    def read1(self, *args):
        data = self._raw.read1(*args)
        self._cost.record(bytes_read=len(data))
        return data

    def readinto(self, buffer):
        count = self._raw.readinto(buffer)
        self._cost.record(bytes_read=count or 0)
        return count

    def readline(self, *args):
        data = self._raw.readline(*args)
        self._cost.record(bytes_read=len(data))
        return data

    def readlines(self, *args):
        lines = self._raw.readlines(*args)
        self._cost.record(bytes_read=sum(len(line) for line in lines))
        return lines

    def __iter__(self):
        for line in self._raw:
            self._cost.record(bytes_read=len(line))
            yield line

    def __getattr__(self, name):
        if name in self._PASSTHROUGH:
            return getattr(self._raw, name)
        raise AttributeError(
            f"{name!r} would bypass the byte count; add counting for it to "
            "_CountingReader"
        )


def _is_bytes(value) -> bool:
    return isinstance(value, (bytes, bytearray, memoryview))


def _rust_patches(cost: SymbolicationCost):
    """Patches wrapping every public callable of gt_rust.symbolic."""

    def counted(name, func):
        def wrapper(*args, **kwargs):
            size = sum(len(arg) for arg in args if _is_bytes(arg))
            with cost._lock:
                cost.rust_calls[name] += 1
                cost.rust_bytes[name] += size
            return func(*args, **kwargs)

        return wrapper

    patches = []
    for attr_name, value in vars(symbolic).items():
        if attr_name.startswith("_"):
            continue
        if inspect.isclass(value):
            for method_name, descriptor in vars(value).items():
                if method_name.startswith("_"):
                    continue
                qualified = f"{attr_name}.{method_name}"
                if isinstance(descriptor, staticmethod):
                    replacement = staticmethod(counted(qualified, descriptor.__func__))
                elif type(descriptor).__name__ == "method_descriptor":
                    replacement = counted(qualified, descriptor)
                else:  # properties and other data descriptors
                    continue
                patches.append(mock.patch.object(value, method_name, replacement))
        elif callable(value):
            wrapper = counted(attr_name, value)
            # Consumers import these by name, so patch every module holding it.
            for module in list(sys.modules.values()):
                if getattr(module, "__dict__", {}).get(attr_name) is value:
                    patches.append(mock.patch.object(module, attr_name, wrapper))
    return patches


@contextlib.contextmanager
def measure_symbolication(cold: bool = True):
    """Record the storage I/O and parser calls symbolication makes.

    Storage is measured at ``Storage.open`` and ``Storage.save``, the base
    class every backend inherits, so the numbers stay comparable when the
    callers change. Reopening an already-open ``FieldFile`` bypasses the
    storage, so that is recorded as an open too. Every public callable of
    ``gt_rust.symbolic`` is counted in ``rust_calls``.

    ``cold`` empties any registered symbolication cache first (see
    ``CACHE_RESET_HOOKS``).
    """
    if cold:
        for reset in CACHE_RESET_HOOKS:
            reset()

    cost = SymbolicationCost()
    original_open = Storage.open
    original_save = Storage.save
    original_fieldfile_open = FieldFile.open

    def counting_open(storage, name, mode="rb"):
        stored = original_open(storage, name, mode)
        cost.record(bytes_opened=storage.size(name))
        with cost._lock:
            cost.opened.append(name)
            cost.opened_by_storage[type(storage).__name__] += 1
        return DjangoFile(_CountingReader(stored, cost), name=name)

    def counting_save(storage, name, content, max_length=None):
        cost.record(files_written=1, bytes_written=content.size or 0)
        return original_save(storage, name, content, max_length=max_length)

    def counting_fieldfile_open(field_file, mode="rb"):
        existing = getattr(field_file, "_file", None)
        if existing is not None:
            # Django reopens an existing file in place (seek, or a raw open()
            # of a local path) without going through the storage.
            cost.record(bytes_opened=field_file.storage.size(field_file.name))
            with cost._lock:
                cost.opened.append(field_file.name)
                cost.reopens.append(field_file.name)
                cost.opened_by_storage[type(field_file.storage).__name__] += 1
        result = original_fieldfile_open(field_file, mode)
        stream = field_file._file.file
        if not isinstance(stream, _CountingReader):
            field_file._file.file = _CountingReader(stream, cost)
        return result

    original_resolve = StacktraceProcessor.resolve_stacktrace.__func__
    signature = inspect.signature(original_resolve)

    def counting_resolve_stacktrace(cls, *args, **kwargs):
        debug_id = signature.bind(cls, *args, **kwargs).arguments.get("debug_id")
        with cost._lock:
            cost.resolver_attempts.append(debug_id)
        return original_resolve(cls, *args, **kwargs)

    cache_counters = {
        "hit": "cache_hits",
        "miss": "cache_misses",
        "disk_hit": "cache_disk_hits",
        "storage_load": "cache_storage_loads",
        "storage_map": "cache_storage_maps",
        "shared_load": "cache_shared_loads",
        "build": "cache_builds",
        "unavailable": "cache_unavailable",
    }

    def observe(event):
        cost.record(**{cache_counters[event]: 1})

    with contextlib.ExitStack() as stack:
        symbolication_store.add_observer(observe)
        stack.callback(symbolication_store.remove_observer, observe)
        stack.enter_context(mock.patch.object(Storage, "open", counting_open))
        stack.enter_context(mock.patch.object(Storage, "save", counting_save))
        stack.enter_context(
            mock.patch.object(FieldFile, "open", counting_fieldfile_open)
        )
        stack.enter_context(
            mock.patch.object(
                StacktraceProcessor,
                "resolve_stacktrace",
                classmethod(counting_resolve_stacktrace),
            )
        )
        for patch in _rust_patches(cost):
            stack.enter_context(patch)
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

    # Fail here, not in a golden, if the upload did not create what a real
    # one would: one bundle per minified file, with its sourcemap attached
    # when one was uploaded alongside it.
    map_names = {a["name"] for a in artifacts if a["type"] == "source_map"}
    for artifact in artifacts:
        if artifact["type"] != "minified_source":
            continue
        bundles = DebugSymbolBundle.objects.filter(
            organization=organization, file__name=artifact["name"]
        ).select_related("sourcemap_file", "release")
        debug_id = artifact.get("headers", {}).get("debug-id")
        if debug_id:
            bundles = bundles.filter(debug_id=debug_id)
        if release:
            bundles = bundles.filter(release__version=release)
        bundle = bundles.get()
        expected_map = artifact.get("headers", {}).get("sourcemap") or artifact.get(
            "headers", {}
        ).get("Sourcemap")
        if expected_map in map_names:
            assert bundle.sourcemap_file is not None, artifact["name"]
            assert bundle.sourcemap_file.name == expected_map, artifact["name"]
        else:
            assert bundle.sourcemap_file is None, artifact["name"]


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


def upload_native_dif(
    project, name: str, content: bytes, kind: str, debug_id: str
) -> DebugInformationFile:
    """Upload a native debug file or source bundle the way ``difs_assemble``
    does, and check it was stored with the expected ``kind`` and debug id."""
    blob = store_blob(name, content)
    file = difs_create_file_from_chunks(name, blob.checksum, [blob.checksum])
    difs_create_difs(project, name, file)
    difs = list(DebugInformationFile.objects.filter(project=project, file=file))
    assert len(difs) == 1, f"{name}: expected one debug file, got {difs}"
    [dif] = difs
    assert dif.data["kind"] == kind, (name, dif.data)
    assert dif.data["debug_id"] == debug_id, (name, dif.data)
    return dif


def upload_copies(project, original: DebugInformationFile, content: bytes, debug_ids):
    """Store one more debug file shaped like ``original`` per entry of
    ``debug_ids``, recorded under that debug id.

    Each copy is a distinct file (``content`` with a different 4-byte tail,
    as a rebuild or a differently stripped copy would be). The rows copy
    ``original``'s metadata as the upload path records it, without parsing
    every copy.
    """
    for index, debug_id in enumerate(debug_ids):
        name = f"{original.name}-{index:04d}"
        blob = store_blob(name, content + index.to_bytes(4, "little"))
        file = difs_create_file_from_chunks(name, blob.checksum, [blob.checksum])
        DebugInformationFile.objects.create(
            project=project,
            name=name,
            file=file,
            data={**original.data, "debug_id": debug_id},
        )


def upload_native_project(project, distinct_debug_ids: int, uploads_per_debug_id: int):
    """The shape of a project that has uploaded native debug files for a
    while: many builds, each uploaded several times (CI retries, per-variant
    uploads). One debug id is the real ELF fixture's, and its first upload is
    the real file. Returns that DIF."""
    elf = (NATIVE_FIXTURES / "crash.elf").read_bytes()
    real = upload_native_dif(project, "crash.elf", elf, "lib", ELF_DEBUG_ID)
    other_ids = [
        str(uuid.UUID(int=index + 1)) for index in range(distinct_debug_ids - 1)
    ]
    # The real file is the first upload of its debug id; the rest are copies.
    upload_copies(
        project,
        real,
        elf,
        [ELF_DEBUG_ID] * (uploads_per_debug_id - 1)
        + [debug_id for debug_id in other_ids for _ in range(uploads_per_debug_id)],
    )
    return real


def upload_crash_sources(project, debug_id: str) -> DebugInformationFile:
    """Upload a source bundle holding crash.cpp for ``debug_id``."""
    return upload_native_dif(
        project,
        f"{debug_id}.src.zip",
        make_source_bundle(
            debug_id, {"/src/native/crash.cpp": CRASH_SOURCE.read_text()}
        ),
        "src",
        debug_id,
    )


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


def native_symbols() -> dict[str, dict[str, dict[str, int]]]:
    """Function starts and crash frame addresses per object format, as
    offsets from the image base (written by the generator)."""
    return json.loads((NATIVE_FIXTURES / "symbols.json").read_text())


def crash_frame_offsets(fmt: str) -> list[int]:
    """Offsets of the crash's frames in ``fmt``, outermost first."""
    frames = native_symbols()[fmt]["frames"]
    return [frames[name] for name in ("main", "run_job", "crash_here")]


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
