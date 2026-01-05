import contextlib
import logging
import tempfile
from hashlib import sha1

from celery import shared_task
from symbolic import Archive

from apps.difs.models import DebugInformationFile
from apps.difs.stacktrace_processor import StacktraceProcessor
from apps.event_ingest.schema import ErrorIssueEventSchema
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


@shared_task
def difs_assemble(project_slug, name, checksum, chunks, debug_id):
    try:
        project = Project.objects.filter(slug=project_slug).get()

        file = difs_get_file_from_chunks(checksum, chunks)
        if file is None:
            file = difs_create_file_from_chunks(name, checksum, chunks)

        difs_create_difs(project, name, file)

    except ChecksumMismatched:
        getLogger().error("difs_assemble: Checksum mismatched: %s", name)
    except Exception as err:
        getLogger().error("difs_assemble: %s", err)


def event_difs_resolve_stacktrace(event: ErrorIssueEventSchema, project_id: int):
    difs = DebugInformationFile.objects.filter(project_id=project_id).order_by(
        "-created"
    )
    resolved_stracktrackes = []
    event_json = event.dict()

    for dif in difs:
        if StacktraceProcessor.is_supported(event_json, dif) is False:
            continue
        blobs = [dif.file.blob]
        with difs_concat_file_blobs_to_disk(blobs) as symbol_file:
            remapped_stacktrace = StacktraceProcessor.resolve_stacktrace(
                event_json, symbol_file.name
            )
            if remapped_stacktrace is not None and remapped_stacktrace.score > 0:
                resolved_stracktrackes.append(remapped_stacktrace)
    if len(resolved_stracktrackes) > 0:
        best_remapped_stacktrace = max(
            resolved_stracktrackes, key=lambda item: item.score
        )
        update_frames(event, best_remapped_stacktrace.frames)


def update_frames(event: ErrorIssueEventSchema, frames):
    # This should be rewritten
    try:
        new_frames = [StackTraceFrame(**frame) for frame in frames]
        event.exception.values[0].stacktrace.frames = new_frames
    except Exception as e:
        getLogger().error(f"StacktraceProcessor: Unexpected error: {e}")
        pass


def difs_get_file_from_chunks(checksum, chunks):
    files = File.objects.filter(checksum=checksum)

    for file in files:
        blob = file.blob
        file_chunks = [blob.checksum]
        if file_chunks == chunks:
            return file

    return None


def difs_create_file_from_chunks(name, checksum, chunks):
    blobs = FileBlob.objects.filter(checksum__in=chunks)

    total_checksum = sha1(b"")
    size = 0

    for blob in blobs:
        size = size + blob.blob.size

        with open(blob.blob.path, "rb") as binary_file:
            content = binary_file.read()
            total_checksum.update(content)

    total_checksum = total_checksum.hexdigest()
    if checksum != total_checksum:
        raise ChecksumMismatched()

    file = File(name=name, headers={}, size=size, checksum=checksum)
    file.blob = blobs[0]
    file.save()
    return file


@contextlib.contextmanager
def difs_concat_file_blobs_to_disk(blobs):
    output = tempfile.NamedTemporaryFile()
    for blob in blobs:
        with open(blob.blob.path, "rb") as binary_file:
            content = binary_file.read()
            output.write(content)

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
                    "debug_id": obj.debug_id,
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
