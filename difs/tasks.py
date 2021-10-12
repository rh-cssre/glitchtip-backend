import tempfile
import contextlib
import logging
from celery import shared_task
from projects.models import Project
from files.models import FileBlob, File
from hashlib import sha1
from symbolic import (
    Archive,
)
from .models import DebugInformationFile


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
        project = Project.objects.filter(
            slug=project_slug
        ).get()

        file = difs_get_file_from_chunks(checksum, chunks)
        if file is None:
            file = difs_create_file_from_chunks(name, checksum, chunks)

        difs_create_difs(project, name, file)

    except ChecksumMismatched:
        getLogger().error(f"difs_assemble: Checksum mismatched: {name}")
    except Exception as e:
        getLogger().error(f"difs_assemble: {e}")


def difs_get_file_from_chunks(checksum, chunks):
    files = File.objects.filter(checksum=checksum)

    for file in files:
        blobs = file.blobs.all()
        file_chunks = [blob.checksum for blob in blobs]
        if file_chunks == chunks:
            return file

    return None


def difs_create_file_from_chunks(name, checksum, chunks):
    blobs = FileBlob.objects.filter(checksum__in=chunks)

    total_checksum = sha1(b'')
    size = 0

    for blob in blobs:
        size = size + blob.blob.size

        with open(blob.blob.path, "rb") as binary_file:
            content = binary_file.read()
            total_checksum.update(content)

    total_checksum = total_checksum.hexdigest()
    if checksum != total_checksum:
        raise ChecksumMismatched()

    file = File(
        name=name,
        headers={},
        size=size,
        checksum=checksum
    )
    file.save()
    file.blobs.set(blobs)
    return file


@contextlib.contextmanager
def difs_concat_file_blobs_to_disk(blobs):
    output = tempfile.NamedTemporaryFile(delete=False)
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
    with difs_concat_file_blobs_to_disk(file.blobs.all()) as input:
        # Only one kind of file format is supported now
        try:
            archive = Archive.open(input.name)
        except Exception as e:
            getLogger().error(f"Extract metadata error: {e}")
            raise UnsupportedFile()
        else:
            return [
                {
                    "arch": obj.arch,
                    "file_format": obj.file_format,
                    "code_id": obj.code_id,
                    "debug_id": obj.debug_id,
                    "kind": obj.kind,
                    "features": list(obj.features)
                }
                for obj in archive.iter_objects()
            ]


def difs_create_difs(project, name, file):
    metadatalist = difs_extract_metadata_from_file(file)
    for metadata in metadatalist:
        dif = DebugInformationFile.objects.filter(
            project_id=project.id,
            file=file
        ).first()

        if dif is not None:
            continue

        code_id = metadata["code_id"]
        debug_id = metadata["debug_id"]
        arch = metadata["arch"]
        kind = metadata["kind"]
        features = metadata["features"]

        dif = DebugInformationFile(
            project=project,
            name=name,
            file=file,
            data={
                "arch": arch,
                "debug_id": debug_id,
                "code_id": code_id,
                "kind": kind,
                "features": features
            }
        )
        dif.save()
