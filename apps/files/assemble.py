"""Partial port of sentry/tasks/assemble.py"""

import hashlib
import json
import logging
import shutil
import tempfile
from enum import Enum
from os import path

from django.core.cache import cache

from apps.organizations_ext.models import Organization
from apps.releases.models import Release
from apps.sourcecode.models import DebugSymbolBundle
from sentry.utils.zip import safe_extract_zip

from .exceptions import AssembleChecksumMismatch
from .models import File, FileBlob

logger = logging.getLogger("glitchtip.files")

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

    bundle_file, temp_file = rv
    scratchpad = tempfile.mkdtemp()
    files: list[File] = []

    def _fail_and_cleanup(detail: str):
        set_assemble_status(
            AssembleTask.ARTIFACTS,
            organization.pk,
            checksum,
            ChunkFileState.ERROR,
            detail=detail,
        )
        if files:
            File.objects.filter(id__in=[f.id for f in files]).delete()
        shutil.rmtree(scratchpad)
        bundle_file.delete()

    try:
        safe_extract_zip(temp_file, scratchpad, strip_toplevel=False)
    except Exception:
        # Catch broadly: zipfile raises BadZipFile, but other I/O errors are
        # possible. All mean the uploaded blob is not a usable zip bundle.
        logger.warning(
            "assemble_artifacts: invalid zip bundle for org %s checksum %s",
            organization.pk,
            checksum,
            exc_info=True,
        )
        _fail_and_cleanup(
            "Failed to extract bundle: uploaded file is not a valid zip archive"
        )
        return

    try:
        manifest_path = path.join(scratchpad, "manifest.json")
        with open(manifest_path, "rb") as manifest:
            manifest = json.loads(manifest.read())
    except Exception:
        # Missing or malformed manifest.json inside the zip.
        logger.warning(
            "assemble_artifacts: bad manifest for org %s checksum %s",
            organization.pk,
            checksum,
            exc_info=True,
        )
        _fail_and_cleanup("Failed to open release manifest")
        return

    if organization.slug != manifest.get("org"):
        _fail_and_cleanup("Organization does not match uploaded bundle")
        return

    release_name = manifest.get("release")
    if release_name != version:
        _fail_and_cleanup("Release does not match uploaded bundle")
        return

    release: Release | None = None
    if release_name:
        release, _ = Release.objects.get_or_create(
            organization=organization, version=release_name
        )

    # Sentry OSS would add dist to release here

    artifacts = manifest.get("files", {})
    for rel_path, artifact in artifacts.items():
        full_path = path.normpath(path.join(scratchpad, rel_path))
        if not full_path.startswith(path.normpath(scratchpad) + path.sep):
            _fail_and_cleanup("Invalid path in manifest")
            return

        artifact_url = artifact.get("url", rel_path)
        artifact_basename = artifact_url.rsplit("/", 1)[-1]
        headers = artifact.get("headers", {})

        file = File.objects.create(
            name=artifact_basename,
            type=artifact["type"],
            headers=headers,
        )
        files.append(file)

        with open(full_path, "rb") as fp:
            file.putfile(fp)

    # Build bundles, deduplicating by debug_id. When a minified_source and its
    # source_map share the same debug-id, keep only the minified_source bundle
    # (which carries the sourcemap_file reference).
    bundles_by_debug_id: dict[str, DebugSymbolBundle] = {}
    bundles_without_debug_id: list[DebugSymbolBundle] = []
    for file in files:
        sourcemap_file = None
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
                pass

        bundle = DebugSymbolBundle(
            organization=organization,
            debug_id=file.headers.get("debug-id"),
            release=release,
            sourcemap_file=sourcemap_file,
            file=file,
        )
        debug_id = bundle.debug_id
        if debug_id is None:
            bundles_without_debug_id.append(bundle)
        elif debug_id not in bundles_by_debug_id or sourcemap_file is not None:
            bundles_by_debug_id[debug_id] = bundle

    bundles = list(bundles_by_debug_id.values()) + bundles_without_debug_id

    # Split bundles: complete bundles (with sourcemap_file) can upsert to
    # replace stale data. Incomplete bundles (no sourcemap_file) should only
    # insert if the debug_id doesn't exist yet — partial re-uploads from the
    # SDK must not overwrite a complete bundle.
    complete_bundles = [b for b in bundles if b.sourcemap_file is not None]
    incomplete_bundles = [b for b in bundles if b.sourcemap_file is None]

    # Collect old file IDs before upsert so we can clean them up
    old_file_ids: set[int] = set()
    if complete_bundles:
        complete_debug_ids = [b.debug_id for b in complete_bundles if b.debug_id]
        if complete_debug_ids:
            for file_id, sm_id in DebugSymbolBundle.objects.filter(
                organization=organization, debug_id__in=complete_debug_ids
            ).values_list("file_id", "sourcemap_file_id"):
                if file_id:
                    old_file_ids.add(file_id)
                if sm_id:
                    old_file_ids.add(sm_id)

        DebugSymbolBundle.objects.bulk_create(
            complete_bundles,
            update_conflicts=True,
            unique_fields=["organization", "debug_id"],
            update_fields=["file", "sourcemap_file", "release"],
        )

    if incomplete_bundles:
        DebugSymbolBundle.objects.bulk_create(
            incomplete_bundles,
            ignore_conflicts=True,
        )

    # Clean up replaced files
    if old_file_ids:
        new_file_ids = {b.file_id for b in bundles if b.file_id} | {
            b.sourcemap_file_id for b in bundles if b.sourcemap_file_id
        }
        orphaned_ids = old_file_ids - new_file_ids
        if orphaned_ids:
            File.objects.filter(id__in=orphaned_ids).delete()

    set_assemble_status(
        AssembleTask.ARTIFACTS, organization.pk, checksum, ChunkFileState.OK
    )
    shutil.rmtree(scratchpad)
    bundle_file.delete()


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
    file_size = sum(x[2] for x in file_blobs if x[2] is not None)
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
