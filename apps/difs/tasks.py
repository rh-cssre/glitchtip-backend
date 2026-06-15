import contextlib
import logging
import tempfile
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.core.files import File as DjangoFile
from django.tasks import task
from symbolic import Archive, normalize_debug_id, parse_addr

from apps.difs.models import DebugInformationFile
from apps.difs.stacktrace_processor import StacktraceProcessor
from apps.event_ingest.schema import (
    ErrorIssueEventSchema,
    JvmDebugImage,
    NativeDebugImage,
)
from apps.files.models import File, FileBlob
from apps.projects.models import Project
from apps.shared.schema.exception import StackTraceFrame


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


def event_difs_resolve_stacktrace(event: ErrorIssueEventSchema, project_id: int):
    # Serialize once for is_android check; native/proguard resolution may
    # mutate this dict but we re-serialize from the Pydantic event below.
    event_json = event.model_dump(mode="json")
    is_android = StacktraceProcessor.is_android_event(event_json)
    native_frames = StacktraceProcessor.has_native_frames(event_json)

    # Filter DIFs at DB level: exclude source bundles (handled separately by
    # resolve_jvm_source_context), and only fetch the relevant symbol type.
    difs = DebugInformationFile.objects.filter(project_id=project_id).exclude(
        data__kind__in=["src", "sources"]
    )
    if is_android and not native_frames:
        difs = difs.filter(data__symbol_type="proguard")
    else:
        difs = difs.exclude(data__symbol_type="proguard")
        native_debug_ids = _extract_native_debug_ids(event)
        if native_debug_ids:
            difs = difs.filter(data__debug_id__in=native_debug_ids)
    difs = difs.select_related("file", "file__blob").order_by("-created")

    # Detect 64-bit addresses to skip 32-bit DIFs that can't match.
    _is_64bit = False
    if native_frames:
        for value in (event_json.get("exception") or {}).get("values", []):
            for frame in (value.get("stacktrace") or {}).get("frames", []):
                addr = frame.get("instruction_addr")
                if addr:
                    try:
                        if parse_addr(addr) > 0xFFFFFFFF:
                            _is_64bit = True
                            break
                    except Exception:
                        pass
            if _is_64bit:
                break

    _32bit_archs = {"arm", "x86", "mips", "ppc"}
    resolved_stracktrackes = []

    for dif in difs:
        if _is_64bit and dif.data.get("arch") in _32bit_archs:
            continue
        blobs = [dif.file.blob]
        with difs_concat_file_blobs_to_disk(blobs) as symbol_file:
            remapped_stacktrace = StacktraceProcessor.resolve_stacktrace(
                event_json,
                symbol_file.name,
                project_id=project_id,
                debug_id=dif.data.get("debug_id"),
            )
            if remapped_stacktrace is not None and remapped_stacktrace.score > 0:
                resolved_stracktrackes.append(remapped_stacktrace)

    if len(resolved_stracktrackes) > 0:
        best_remapped_stacktrace = max(
            resolved_stracktrackes,
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

        update_frames(event, best_remapped_stacktrace.frames)

    # JVM source context (runs after proguard deobfuscation if applicable).
    # Re-serialize from the Pydantic event to pick up any frame changes from
    # proguard deobfuscation above.
    jvm_debug_ids = _extract_jvm_debug_ids(event)
    if jvm_debug_ids:
        event_json = event.model_dump(mode="json")
        if StacktraceProcessor.resolve_jvm_source_context(
            event_json, project_id, jvm_debug_ids
        ):
            _update_source_context_on_event(event, event_json)


def update_frames(event: ErrorIssueEventSchema, frames):
    # This should be rewritten
    try:
        new_frames = [StackTraceFrame(**frame) for frame in frames if frame is not None]
        event.exception.values[0].stacktrace.frames = new_frames
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


@contextlib.contextmanager
def difs_concat_file_blobs_to_disk(blobs):
    output = tempfile.NamedTemporaryFile()
    for blob in blobs:
        with blob.blob.open("rb") as binary_file:
            output.write(binary_file.read())

    output.flush()
    output.seek(0)
    try:
        yield output
    finally:
        output.close()


def difs_extract_metadata_from_file(file):
    with difs_concat_file_blobs_to_disk([file.blob]) as _input:
        # Only one kind of file format is supported now
        try:
            archive = Archive.open(_input.name)
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
