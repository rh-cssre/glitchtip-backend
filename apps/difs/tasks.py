import asyncio
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
from apps.difs.proguard import (
    _PROGUARD_SNIFF_BYTES,
    extract_proguard_id,
    extract_proguard_metadata,
)
from apps.difs.stacktrace_processor import ResolvedStacktrace, StacktraceProcessor
from apps.event_ingest.schema import (
    ErrorIssueEventSchema,
    JvmDebugImage,
    NativeDebugImage,
    ProguardDebugImage,
)
from apps.files.assemble import AssembleTask, ChunkFileState, set_assemble_status
from apps.files.models import File, FileBlob
from apps.projects.models import Project
from apps.shared.schema.exception import StackTraceFrame
from apps.symbolication.models import CacheKind
from apps.symbolication.sources import blob_input
from apps.symbolication.store import CacheSource, LookupContext, get_cache


def getLogger():
    return logging.getLogger("glitchtip.difs")


class ChecksumMismatched(Exception):
    pass


class UnsupportedFile(Exception):
    pass


DIF_STATE_CREATED = "created"
DIF_STATE_OK = "ok"
DIF_STATE_NOT_FOUND = "not_found"
DIF_STATE_ERROR = "error"


@task
async def difs_assemble(project_id, name, checksum, chunks, debug_id):
    try:
        project = await Project.objects.aget(id=project_id)

        file = await sync_to_async(difs_get_file_from_chunks)(checksum, chunks)
        if file is None:
            file = await sync_to_async(difs_create_file_from_chunks)(
                name, checksum, chunks
            )

        await sync_to_async(difs_create_difs)(project, name, file, debug_id)

    except ChecksumMismatched:
        getLogger().error("difs_assemble: Checksum mismatched: %s", name)
        await _assemble_failed(project_id, checksum, "checksum mismatch")
    except UnsupportedFile:
        getLogger().warning("difs_assemble: unsupported debug file: %s", name)
        await _assemble_failed(
            project_id,
            checksum,
            "not a supported debug file (or a ProGuard mapping without its uuid)",
        )
    except Exception as err:
        getLogger().error("difs_assemble: %s", type(err).__name__)
        await _assemble_failed(project_id, checksum, "assemble failed")


async def _assemble_failed(project_id, checksum, detail: str):
    """Record the failure, so the upload tool's next assemble poll reports
    an error instead of the upload quietly never appearing."""
    await sync_to_async(set_assemble_status)(
        AssembleTask.DIF, project_id, checksum, ChunkFileState.ERROR, detail=detail
    )


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
        getLogger().error(f"_update_source_context_on_event: {type(e)}")


# Rows read when choosing candidate debug files for an event.
FALLBACK_SCAN_ROWS = 200
# Exceptions of one event symbolicated at most.
MAX_EXCEPTIONS = 10
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
    """The debug files to try: one per debug id, newest first, at most
    GLITCHTIP_SYMBOLICATION_MAX_CANDIDATES however many ids the event names.

    With debug ids (from the event), every id's newest upload that carries
    debug info wins over a newer symbols-only one. Without them, the newest
    GLITCHTIP_SYMBOLICATION_FALLBACK_CANDIDATES distinct debug ids are tried,
    reading a bounded number of rows. ``accept`` filters out debug files that
    cannot match the event (e.g. 32-bit files for a 64-bit crash).
    """
    queryset = queryset.select_related("file__blob").order_by("-created")
    cap = settings.GLITCHTIP_SYMBOLICATION_MAX_CANDIDATES
    if debug_ids:
        limit = min(len(debug_ids), cap)
        queryset = queryset.filter(data__debug_id__in=debug_ids)[:FALLBACK_SCAN_ROWS]
    else:
        limit = min(settings.GLITCHTIP_SYMBOLICATION_FALLBACK_CANDIDATES, cap)
        queryset = queryset[:FALLBACK_SCAN_ROWS]
    if limit <= 0:
        return []
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


def _rank(resolved: ResolvedStacktrace):
    """Which candidate's result an exception keeps: most frames resolved,
    then most with a source file."""
    return (
        resolved.score,
        sum(1 for f in resolved.frames if f and f.get("filename")),
    )


async def resolve_event_stacktraces(
    event: ErrorIssueEventSchema,
    project_id: int,
    context: LookupContext | None = None,
):
    """Symbolicate an event's native or ProGuard frames, and add JVM source
    context, using stored caches of the project's debug files."""
    context = context or LookupContext.for_event()
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

    # Chained exceptions each carry their own stack trace; resolve each (up
    # to a bound: the payload decides how many there are).
    exception_indexes = [
        index
        for index, value in enumerate(
            (event_json.get("exception") or {}).get("values") or []
        )
        if value and value.get("stacktrace")
    ][:MAX_EXCEPTIONS]
    caches = await asyncio.gather(
        *(get_cache(_cache_source(dif, kind), context) for dif in candidates)
    )
    best: dict[int, tuple] = {}
    for dif, cache in zip(candidates, caches):
        if cache is None:
            continue
        debug_id = dif.data.get("debug_id")
        estimated_base = None
        if kind == CacheKind.SYMCACHE:
            estimated_base = await StacktraceProcessor.estimate_base_for_event(
                event_json, cache, debug_id, exception_indexes
            )
        for index in exception_indexes:
            remapped_stacktrace = StacktraceProcessor.resolve_stacktrace(
                event_json,
                cache,
                debug_id=debug_id,
                exception_index=index,
                estimated_base=estimated_base,
            )
            if remapped_stacktrace is None or remapped_stacktrace.score <= 0:
                continue
            if index not in best or _rank(remapped_stacktrace) > _rank(best[index][0]):
                best[index] = (remapped_stacktrace, debug_id)

    # Source context for the winners only: one source bundle lookup and one
    # read per debug id, whatever the number of frames and exceptions.
    by_debug_id: dict = {}
    for remapped_stacktrace, debug_id in best.values():
        by_debug_id.setdefault(debug_id, []).append(remapped_stacktrace)
    for debug_id, results in by_debug_id.items():
        await StacktraceProcessor.add_native_source_context(
            results, project_id, debug_id
        )
    for index, (remapped_stacktrace, _) in best.items():
        update_frames(event, remapped_stacktrace.frames, index)

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
        # The error text of a validation error quotes the event's values.
        getLogger().error(f"StacktraceProcessor: could not update frames: {type(e)}")


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
    with blob_input(file.blob) as source:
        # Only one kind of file format is supported now
        try:
            archive = (
                Archive.open(source)
                if isinstance(source, str)
                else Archive.from_bytes(source)
            )
        except Exception as err:
            getLogger().info("Extract metadata error: %s", type(err).__name__)
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


def _proguard_mapping_metadata(file, name: str, debug_id: str | None):
    """The DIF data for a ProGuard mapping uploaded by chunks, as the
    dsyms upload stores it, or None if ``file`` is not a mapping or its
    uuid is unknown (upload tools send it as the debug id)."""
    with file.blob.blob.open("rb") as stored:
        head = stored.read(_PROGUARD_SNIFF_BYTES)
    if extract_proguard_metadata(head) is None:
        return None
    mapping_uuid = debug_id or extract_proguard_id(name)
    if not mapping_uuid:
        return None
    try:
        mapping_uuid = normalize_debug_id(mapping_uuid)
    except Exception:
        return None
    return {
        "arch": "any",
        "debug_id": mapping_uuid,
        "symbol_type": "proguard",
        "features": ["mapping"],
    }


def difs_create_difs(project, name, file, debug_id=None):
    try:
        metadatalist = difs_extract_metadata_from_file(file)
    except UnsupportedFile:
        proguard = _proguard_mapping_metadata(file, name, debug_id)
        if proguard is None:
            raise
        if not DebugInformationFile.objects.filter(
            project_id=project.id, file=file
        ).exists():
            DebugInformationFile.objects.create(
                project=project, name=name, file=file, data=proguard
            )
        return
    # One row per object: a universal (fat) Mach-O holds one per
    # architecture, each with its own debug id.
    existing = set(
        DebugInformationFile.objects.filter(
            project_id=project.id, file=file
        ).values_list("data__debug_id", flat=True)
    )
    for metadata in metadatalist:
        if metadata["debug_id"] in existing:
            continue
        existing.add(metadata["debug_id"])

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
