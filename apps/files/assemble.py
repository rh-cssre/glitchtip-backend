"""Partial port of sentry/tasks/assemble.py"""

import hashlib
import json
import shutil
import tempfile
from enum import Enum
from os import path

from django.core.cache import cache

from apps.organizations_ext.models import Organization
from apps.releases.models import Release
from apps.sourcecode.models import DebugSymbolBundle
from sentry.utils.zip import safe_extract_zip

from .exceptions import AssembleArtifactsError, AssembleChecksumMismatch
from .models import File, FileBlob

MAX_FILE_SIZE = 2**31  # 2GB is the maximum offset supported by fileblob


class ChunkFileState(Enum):
    OK = "ok"  # File in database
    NOT_FOUND = "not_found"  # File not found in database
    CREATED = "created"  # File was created in the request and send to the worker for assembling
    ASSEMBLING = "assembling"  # File still being processed by worker
    ERROR = "error"  # Error happened during assembling


class AssembleTask(Enum):
    DIF = "project.dsym"  # Debug file upload
    ARTIFACTS = "organization.artifacts"  # Release file upload


def _get_cache_key(task, scope, checksum):
    """Computes the cache key for assemble status.

    ``task`` must be one of the ``AssembleTask`` values. The scope can be the
    identifier of any model, such as the organization or project that this task
    is performed under.

    ``checksum`` should be the SHA1 hash of the main file that is being
    assembled.
    """
    return (
        "assemble-status:%s"
        % hashlib.sha1(
            ("%s|%s|%s" % (scope, checksum.encode("ascii"), task)).encode()
        ).hexdigest()
    )


def set_assemble_status(
    task: AssembleTask, scope, checksum, state: ChunkFileState, detail=None
):
    """
    Updates the status of an assembling task. It is cached for 10 minutes.
    """
    cache_key = _get_cache_key(task, scope, checksum)
    cache.set(cache_key, (state, detail), 600)


def assemble_artifacts(
    organization: Organization, version: str | None, checksum: str, chunks: list[str]
):
    set_assemble_status(
        AssembleTask.ARTIFACTS, organization.pk, checksum, ChunkFileState.ASSEMBLING
    )
    # Assemble the chunks into a temporary file
    rv = assemble_file(
        AssembleTask.ARTIFACTS,
        organization,
        "release-artifacts.zip",
        checksum,
        chunks,
        file_type="release.bundle",
    )

    if rv is None:
        return

    bundle, temp_file = rv
    scratchpad = tempfile.mkdtemp()

    try:
        safe_extract_zip(temp_file, scratchpad, strip_toplevel=False)
    except BaseException as ex:
        raise AssembleArtifactsError("failed to extract bundle") from ex

    try:
        manifest_path = path.join(scratchpad, "manifest.json")
        with open(manifest_path, "rb") as manifest:
            manifest = json.loads(manifest.read())
    except BaseException as ex:
        raise AssembleArtifactsError("failed to open release manifest") from ex

    if organization.slug != manifest.get("org"):
        raise AssembleArtifactsError("organization does not match uploaded bundle")

    release_name = manifest.get("release")
    if release_name != version:
        raise AssembleArtifactsError("release does not match uploaded bundle")

    release: Release | None = None
    if release_name:
        release, _ = Release.objects.get_or_create(
            organization=organization, version=release_name
        )

    # Sentry OSS would add dist to release here

    artifacts = manifest.get("files", {})
    files = []
    for rel_path, artifact in artifacts.items():
        artifact_url = artifact.get("url", rel_path)
        artifact_basename = artifact_url.rsplit("/", 1)[-1]
        headers = artifact.get("headers", {})

        file = File.objects.create(
            name=artifact_basename,
            type=artifact["type"],
            headers=headers,
        )
        files.append(file)

        full_path = path.join(scratchpad, rel_path)
        with open(full_path, "rb") as fp:
            file.putfile(fp)

    bundles: list[DebugSymbolBundle] = []
    for file in files:
        if file.type == "minified_source":
            try:
                sourcemap_file = next(
                    value
                    for value in files
                    if value.type == "source_map"
                    and (
                        file.headers.get("sourcemap", file.headers.get("Sourcemap"))
                        == value.name
                        or (
                            value.headers.get("debug-id")
                            and value.headers.get("debug-id")
                            == file.headers.get("debug-id")
                        )
                    )
                )
            except StopIteration:
                sourcemap_file = None

            if sourcemap_file:
                bundles.append(
                    DebugSymbolBundle(
                        organization=organization,
                        debug_id=file.headers.get("debug-id"),
                        release=release,
                        sourcemap_file=sourcemap_file,
                        file=file,
                    )
                )

    DebugSymbolBundle.objects.bulk_create(
        bundles,
        ignore_conflicts=True,
        # unique_fields=["organization", "debug_id", "release"],
        # update_fields=["file", "sourcemap_file"],
    )
    # May need to readd this logic but in bulk
    # if not created:
    #     old_file = release_file.file
    #     release_file.file = file
    #     release_file.save(update_fields=["file"])
    #     old_file.delete()

    set_assemble_status(
        AssembleTask.ARTIFACTS, organization.pk, checksum, ChunkFileState.OK
    )
    shutil.rmtree(scratchpad)
    bundle.delete()


def assemble_file(
    task: AssembleTask,
    organization: Organization,
    name: str,
    checksum,
    chunks,
    file_type,
):
    """
    Verifies and assembles a file model from chunks.

    This downloads all chunks from blob store to verify their integrity and
    associates them with a created file model. Additionally, it assembles the
    full file in a temporary location and verifies the complete content hash.

    Returns a tuple ``(File, TempFile)`` on success, or ``None`` on error.
    """
    # Load all FileBlobs from db since we can be sure here we already own all
    # chunks need to build the file
    file_blobs = FileBlob.objects.filter(checksum__in=chunks).values_list(
        "id", "checksum", "size"
    )

    # Reject all files that exceed the maximum allowed size for this
    # organization. This value cannot be
    file_size = sum(x[2] for x in file_blobs)
    if file_size > MAX_FILE_SIZE:
        set_assemble_status(
            task,
            organization.id,
            checksum,
            ChunkFileState.ERROR,
            detail="File exceeds maximum size",
        )
        return

    # Sanity check.  In case not all blobs exist at this point we have a
    # race condition.
    if set(x[1] for x in file_blobs) != set(chunks):
        set_assemble_status(
            task,
            organization.id,
            checksum,
            ChunkFileState.ERROR,
            detail="Not all chunks available for assembling",
        )
        return

    # Ensure blobs are in the order and duplication in which they were
    # transmitted. Otherwise, we would assemble the file in the wrong order.
    ids_by_checksum = {chks: id for id, chks, _ in file_blobs}
    file_blob_ids = [ids_by_checksum[c] for c in chunks]

    file = File.objects.create(name=name, checksum=checksum, type=file_type)
    try:
        temp_file = file.assemble_from_file_blob_ids(file_blob_ids, checksum)
    except AssembleChecksumMismatch:
        file.delete()
        set_assemble_status(
            task,
            organization.id,
            checksum,
            ChunkFileState.ERROR,
            detail="Reported checksum mismatch",
        )
    else:
        file.save()
        return file, temp_file
