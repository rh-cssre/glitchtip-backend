import logging
import tempfile
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files import File as DjangoFile
from django.db.models import Q
from django.tasks import task
from gt_rust.symbolic import Archive, normalize_debug_id, parse_addr

from apps.difs.models import DebugInformationFile
from apps.difs.stacktrace_processor import StacktraceProcessor
from apps.event_ingest.schema import (
    ErrorIssueEventSchema,
    JvmDebugImage,
    NativeDebugImage,
    ProguardDebugImage,
)
from apps.files.models import File, FileBlob
from apps.projects.models import Project
from apps.shared.schema.exception import StackTraceFrame
from apps.symbolication.models import CacheKind
from apps.symbolication.sources import blob_path
from apps.symbolication.store import CacheSource, get_cache


def getLogger():
    return logging.getLogger("glitchtip.difs")


class ChecksumMismatched(Exception):
    pass


class UnsupportedFile(Exception):
    pass


DIF_STATE_CREATED = "created"
DIF_STATE_OK = "ok"
DIF_STATE_NOT_FOUND = "not_found"


@task
async def difs_assemble(project_id, name, checksum, chunks, debug_id):
    try:
        project = await Project.objects.aget(id=project_id)

        file = await sync_to_async(difs_get_file_from_chunks)(checksum, chunks)
        if file is None:
            file = await sync_to_async(difs_create_file_from_chunks)(
                name, checksum, chunks
            )

        await sync_to_async(difs_create_difs)(project, name, file)

    except ChecksumMismatched:
        getLogger().error("difs_assemble: Checksum mismatched: %s", name)
    except Exception as err:
        getLogger().error("difs_assemble: %s", err)


def _extract_jvm_debug_ids(event: ErrorIssueEventSchema) -> list[str]:
    """Extract debug_ids from JVM debug images in the event."""
    if not event.debug_meta:
        return []
    return [
        normalize_debug_id(str(image.debug_id))
        for image in event.debug_meta.images
        if isinstance(image, JvmDebugImage)
    ]


def _extract_native_debug_ids(event: ErrorIssueEventSchema) -> list[str]:
    """Extract normalized debug_ids from native debug images."""
    if not event.debug_meta:
        return []
    return [
        normalize_debug_id(str(image.debug_id))
        for image in event.debug_meta.images
        if isinstance(image, NativeDebugImage) and image.debug_id
    ]


def _update_source_context_on_event(event: ErrorIssueEventSchema, event_json: dict):
    """Copy enriched source context from event_json dict back onto the Pydantic event."""
    try:
        json_exceptions = (event_json.get("exception") or {}).get("values", [])
        pydantic_exceptions = event.exception.values
        # Guard against length mismatch between dict and Pydantic representations
        for i, exc_data in enumerate(json_exceptions):
            if i >= len(pydantic_exceptions):
                break
            stacktrace = exc_data.get("stacktrace")
            if not stacktrace:
                continue
            pydantic_stacktrace = pydantic_exceptions[i].stacktrace
            if not pydantic_stacktrace:
                continue
            json_frames = stacktrace.get("frames", [])
            pydantic_frames = pydantic_stacktrace.frames
            for j, json_frame in enumerate(json_frames):
                if json_frame.get("context_line") and j < len(pydantic_frames):
                    pydantic_frames[j].context_line = json_frame["context_line"]
                    pydantic_frames[j].pre_context = json_frame.get("pre_context")
                    pydantic_frames[j].post_context = json_frame.get("post_context")
    except Exception as e:
        getLogger().error(f"_update_source_context_on_event: {e}")


# Rows read when choosing fallback candidates for an event without debug ids.
FALLBACK_SCAN_ROWS = 200
_32BIT_ARCHS = frozenset({"arm", "x86", "mips", "ppc"})


def _extract_proguard_uuids(event: ErrorIssueEventSchema) -> list[str]:
    """Normalized uuids of the ProGuard mappings the event names."""
    if not event.debug_meta:
        return []
    return [
        normalize_debug_id(str(image.uuid))
        for image in event.debug_meta.images
        if isinstance(image, ProguardDebugImage) and image.uuid
    ]


def _is_64bit_event(event_json: dict) -> bool:
    """Whether any native frame's address needs 64 bits, so 32-bit debug
    files cannot match."""
    for value in (event_json.get("exception") or {}).get("values", []):
        for frame in (value.get("stacktrace") or {}).get("frames", []):
            addr = frame.get("instruction_addr")
            if addr:
                try:
                    if parse_addr(addr) > 0xFFFFFFFF:
                        return True
                except Exception:
                    pass
    return False


def _has_debug_info(dif: DebugInformationFile) -> bool:
    return "debug" in ((dif.data or {}).get("features") or [])


async def _candidate_difs(queryset, debug_ids: list[str], accept) -> list:
    """The debug files to try: one per debug id, newest first.

    With debug ids (from the event), every id's newest upload that carries
    debug info wins over a newer symbols-only one. Without them, the newest
    GLITCHTIP_SYMBOLICATION_FALLBACK_CANDIDATES distinct debug ids are tried,
    reading a bounded number of rows. ``accept`` filters out debug files that
    cannot match the event (e.g. 32-bit files for a 64-bit crash).
    """
    queryset = queryset.select_related("file__blob").order_by("-created")
    if debug_ids:
        limit = len(debug_ids)
        queryset = queryset.filter(data__debug_id__in=debug_ids)
    else:
        limit = settings.GLITCHTIP_SYMBOLICATION_FALLBACK_CANDIDATES
        if limit <= 0:
            return []
        queryset = queryset[:FALLBACK_SCAN_ROWS]
    chosen: dict = {}
    async for dif in queryset:
        if dif.file is None or dif.file.blob is None or not accept(dif):
            continue
        debug_id = (dif.data or {}).get("debug_id")
        current = chosen.get(debug_id)
        if current is None:
            if len(chosen) >= limit:
                continue
            chosen[debug_id] = dif
        elif _has_debug_info(dif) and not _has_debug_info(current):
            chosen[debug_id] = dif
    return list(chosen.values())


def _cache_source(dif: DebugInformationFile, kind: CacheKind) -> CacheSource:
    blob = dif.file.blob
    if kind == CacheKind.PROGUARD:
        cache_key = blob.checksum
    else:
        data = dif.data or {}
        cache_key = (
            f"{blob.checksum}:{data.get('debug_id') or ''}:{data.get('arch') or ''}"
        )
    return CacheSource(kind=kind, cache_key=cache_key, source_blob_id=blob.id)


async def resolve_event_stacktraces(event: ErrorIssueEventSchema, project_id: int):
    """Symbolicate an event's native or ProGuard frames, and add JVM source
    context, using stored caches of the project's debug files."""
    # Serialize once for is_android check; native/proguard resolution may
    # mutate this dict but we re-serialize from the Pydantic event below.
    event_json = event.model_dump(mode="json")
    is_android = StacktraceProcessor.is_android_event(event_json)
    native_frames = StacktraceProcessor.has_native_frames(event_json)

    # Source bundles are handled separately by resolve_jvm_source_context
    # and add_native_source_context. The has_key guard matters: in SQL,
    # NOT (data->'kind' IN (...)) is NULL, not true, for a row without a
    # kind, so a bare exclude(data__kind__in=...) would also drop every
    # ProGuard mapping (they have no kind).
    difs = DebugInformationFile.objects.filter(project_id=project_id).exclude(
        Q(data__has_key="kind") & Q(data__kind__in=["src", "sources"])
    )
    if is_android and not native_frames:
        kind = CacheKind.PROGUARD
        candidates = await _candidate_difs(
            difs.filter(data__symbol_type="proguard"),
            _extract_proguard_uuids(event),
            accept=lambda dif: True,
        )
    elif native_frames:
        kind = CacheKind.SYMCACHE
        is_64bit = _is_64bit_event(event_json)
        candidates = await _candidate_difs(
            difs.exclude(data__symbol_type="proguard"),
            _extract_native_debug_ids(event),
            accept=lambda dif: (
                not (is_64bit and (dif.data or {}).get("arch") in _32BIT_ARCHS)
            ),
        )
    else:
        # No instruction addresses and no ProGuard frames: nothing a debug
        # file can resolve.
        candidates = []

    # Chained exceptions each carry their own stack trace; resolve every one.
    exception_indexes = [
        index
        for index, value in enumerate(
            (event_json.get("exception") or {}).get("values") or []
        )
        if value and value.get("stacktrace")
    ]
    resolved: dict[int, list] = {index: [] for index in exception_indexes}
    for dif in candidates:
        cache = await get_cache(_cache_source(dif, kind))
        if cache is None:
            continue
        debug_id = dif.data.get("debug_id")
        for index in exception_indexes:
            remapped_stacktrace = StacktraceProcessor.resolve_stacktrace(
                event_json, cache, debug_id=debug_id, exception_index=index
            )
            if remapped_stacktrace is not None and remapped_stacktrace.score > 0:
                await StacktraceProcessor.add_native_source_context(
                    remapped_stacktrace, project_id, debug_id
                )
                resolved[index].append(remapped_stacktrace)

    for index, results in resolved.items():
        if not results:
            continue
        best_remapped_stacktrace = max(
            results,
            key=lambda item: (
                sum(
                    1
                    for f in item.frames
                    if f
                    and f.get("filename")
                    and f.get("pre_context")
                    and f.get("post_context")
                ),
                item.score,
                sum(1 for f in item.frames if f and f.get("filename")),
            ),
        )
        update_frames(event, best_remapped_stacktrace.frames, index)

    # JVM source context (runs after proguard deobfuscation if applicable).
    # Re-serialize from the Pydantic event to pick up any frame changes from
    # proguard deobfuscation above.
    jvm_debug_ids = _extract_jvm_debug_ids(event)
    if jvm_debug_ids:
        event_json = event.model_dump(mode="json")
        if await StacktraceProcessor.resolve_jvm_source_context(
            event_json, project_id, jvm_debug_ids
        ):
            _update_source_context_on_event(event, event_json)


def update_frames(event: ErrorIssueEventSchema, frames, exception_index=0):
    try:
        new_frames = [StackTraceFrame(**frame) for frame in frames if frame is not None]
        event.exception.values[exception_index].stacktrace.frames = new_frames
    except Exception as e:
        getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")


def difs_get_file_from_chunks(checksum, chunks):
    # File.checksum is the whole-file SHA1, so an existing File with a matching
    # checksum already holds identical content, regardless of how it was split
    # into chunks on upload. (chunks is kept for call-site compatibility.)
    return (
        File.objects.filter(checksum=checksum, blob__isnull=False)
        .select_related("blob")
        .first()
    )


def difs_create_file_from_chunks(name, checksum, chunks):
    # Order blobs by the client-supplied chunk order. filter(__in=...) returns
    # rows in arbitrary order, so we must re-order to reassemble correctly.
    blobs_by_checksum = {
        blob.checksum: blob for blob in FileBlob.objects.filter(checksum__in=chunks)
    }
    if set(blobs_by_checksum) != set(chunks):
        # A chunk is missing; the file cannot be assembled.
        raise ChecksumMismatched()
    ordered_blobs = [blobs_by_checksum[c] for c in chunks]

    # GlitchTip's File model points at a single FileBlob, so a multi-chunk file
    # must be concatenated into one combined blob. Stream the chunks in order
    # into a temp file while verifying the whole-file checksum.
    total_checksum = sha1(b"")
    size = 0
    with tempfile.NamedTemporaryFile() as tf:
        for blob in ordered_blobs:
            with blob.blob.open("rb") as binary_file:
                while data := binary_file.read(65536):
                    size += len(data)
                    total_checksum.update(data)
                    tf.write(data)

        if checksum != total_checksum.hexdigest():
            raise ChecksumMismatched()

        if len(ordered_blobs) == 1:
            # Single chunk: the uploaded blob already is the whole file, so
            # reuse it directly and avoid storing a duplicate blob.
            file_blob = ordered_blobs[0]
        else:
            tf.flush()
            tf.seek(0)
            file_blob, _ = FileBlob.objects.get_or_create(
                checksum=checksum,
                defaults={"blob": DjangoFile(tf, name=checksum), "size": size},
            )

    file = File(name=name, headers={}, size=size, checksum=checksum)
    file.blob = file_blob
    file.save()
    return file


def difs_extract_metadata_from_file(file):
    with blob_path(file.blob) as path:
        # Only one kind of file format is supported now
        try:
            archive = Archive.open(path)
        except Exception as err:
            getLogger().error("Extract metadata error: %s", err)
            raise UnsupportedFile() from err
        else:
            return [
                {
                    "arch": obj.arch,
                    "file_format": obj.file_format,
                    "code_id": obj.code_id,
                    "debug_id": normalize_debug_id(obj.debug_id)
                    if obj.debug_id
                    else obj.debug_id,
                    "kind": obj.kind,
                    "features": list(obj.features),
                    "symbol_type": "native",
                }
                for obj in archive.iter_objects()
            ]


def difs_create_difs(project, name, file):
    metadatalist = difs_extract_metadata_from_file(file)
    for metadata in metadatalist:
        dif = DebugInformationFile.objects.filter(
            project_id=project.id, file=file
        ).first()

        if dif is not None:
            continue

        code_id = metadata["code_id"]
        debug_id = metadata["debug_id"]
        arch = metadata["arch"]
        kind = metadata["kind"]
        features = metadata["features"]
        symbol_type = metadata["symbol_type"]

        dif = DebugInformationFile(
            project=project,
            name=name,
            file=file,
            data={
                "arch": arch,
                "debug_id": debug_id,
                "code_id": code_id,
                "kind": kind,
                "features": features,
                "symbol_type": symbol_type,
            },
        )
        dif.save()
