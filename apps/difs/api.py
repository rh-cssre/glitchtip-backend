"""Port of sentry.api.endpoints.debug_files.DifAssembleEndpoint"""

import contextlib
import io
import lzma
import tempfile
import zipfile
import zlib
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files import File as DjangoFile
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from gt_rust.symbolic import normalize_debug_id
from ninja import File as NinjaFile
from ninja import Router
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja.pagination import paginate

from apps.files.assemble import (
    AssembleTask,
    ChunkFileState,
    get_assemble_status,
    set_assemble_status,
)
from apps.files.models import File, FileBlob
from apps.projects.models import Project
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import OrgScopedHttpRequest, org_scoped
from sentry.utils.zip import ZipExtractionLimitExceeded, _reject_oversized_input

from .models import DebugInformationFile
from .proguard import (
    _PROGUARD_SNIFF_BYTES,
    extract_proguard_id,
    extract_proguard_metadata,
)
from .schema import AssemblePayload, DebugFileSchema
from .tasks import (
    DIF_STATE_CREATED,
    DIF_STATE_ERROR,
    DIF_STATE_NOT_FOUND,
    DIF_STATE_OK,
    chunks_are_not_a_mapping,
    difs_assemble,
)

MAX_UPLOAD_BLOB_SIZE = 32 * 1024 * 1024  # 32MB
_DIF_READ_CHUNK_SIZE = 64 * 1024
# What reading a damaged archive raises: zipfile's own error, but also the
# decompressors' (bz2 raises OSError), truncation, unsupported compression
# methods, encryption (RuntimeError) and undecodable names (ValueError).
_CORRUPT_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    OSError,
    EOFError,
    ValueError,
    NotImplementedError,
    RuntimeError,
)


@contextlib.contextmanager
def _corrupt_zip_is_a_bad_request():
    """For zip reads only: the errors are broad enough that wrapping storage
    or database calls would report their failures as a bad upload."""
    try:
        yield
    except _CORRUPT_ZIP_ERRORS as err:
        raise HttpError(400, "Corrupt zip file uploaded") from err


router = Router()


@optional_slash(
    router,
    "post",
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/files/difs/assemble/",
)
@org_scoped(["project:write", "project:admin", "project:releases"], Project)
async def difs_assemble_api(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    payload: AssemblePayload,
):
    project = await aget_object_or_404(qs, slug=project_slug.lower())

    responses = {}

    files = payload.root.items()

    for checksum, file in files:
        chunks = file.chunks
        name = file.name
        debug_id = file.debug_id
        debug_file = await (
            DebugInformationFile.objects.filter(
                project=project, file__checksum=checksum
            )
            .select_related("file")
            .afirst()
        )

        if debug_file is not None:
            responses[checksum] = {
                "state": DIF_STATE_OK,
                "missingChunks": [],
            }
            continue

        existed_chunks = [
            file_blob
            async for file_blob in FileBlob.objects.filter(
                checksum__in=chunks
            ).values_list("checksum", flat=True)
        ]

        missing_chunks = list(set(chunks) - set(existed_chunks))

        if len(missing_chunks) != 0:
            responses[checksum] = {
                "state": DIF_STATE_NOT_FOUND,
                "missingChunks": missing_chunks,
            }
            continue

        # All chunks are here, but an earlier assemble of this file failed
        # (not a debug file we can read, say): report it, rather than
        # queueing it again and again.
        status = await sync_to_async(get_assemble_status)(
            AssembleTask.DIF, project.id, checksum
        )
        if status is not None and status[0] == ChunkFileState.ERROR:
            responses[checksum] = {
                "state": DIF_STATE_ERROR,
                "missingChunks": [],
                "detail": status[1],
            }
            continue

        if extract_proguard_id(name) and await sync_to_async(chunks_are_not_a_mapping)(
            chunks
        ):
            detail = "not a ProGuard mapping"
            await sync_to_async(set_assemble_status)(
                AssembleTask.DIF,
                project.id,
                checksum,
                ChunkFileState.ERROR,
                detail=detail,
            )
            responses[checksum] = {
                "state": DIF_STATE_ERROR,
                "missingChunks": [],
                "detail": detail,
            }
            continue

        responses[checksum] = {"state": DIF_STATE_CREATED, "missingChunks": []}
        await difs_assemble.aenqueue(project.id, name, checksum, chunks, debug_id)

    return responses


@optional_slash(
    router,
    "post",
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/reprocessing/",
)
@org_scoped(["project:write", "project:admin", "project:releases"], Project)
async def project_reprocessing(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
):
    """
    Not implemented. It is a dummy API to keep `sentry-cli upload-dif` happy
    """
    # Read the project anyway: an unevaluated qs checks no membership.
    await aget_object_or_404(qs.only("id"), slug=project_slug.lower())


def _spool_entry(entry, head: bytes, out, size_budget: int) -> tuple[str, int]:
    """Copy a zip entry to ``out`` in chunks, returning its SHA-1 and size.
    Off the event loop: inflating up to the whole budget takes seconds."""
    hasher = sha1()
    size = 0
    chunk = head
    while chunk:
        size += len(chunk)
        if size > size_budget:
            raise HttpError(400, "Uploaded file expands too large")
        hasher.update(chunk)
        out.write(chunk)
        with _corrupt_zip_is_a_bad_request():
            chunk = entry.read(_DIF_READ_CHUNK_SIZE)
    out.flush()
    return hasher.hexdigest(), size


async def create_dif_from_read_only_file(
    proguard_file, project, proguard_id, filename, size_budget
):
    """Validate one `proguard/<uuid>.txt` zip entry and store it as a DIF.

    The entry is streamed rather than read whole: a zip's compression ratio
    has no ceiling, so `read()` on an uploaded entry is unbounded however
    small the upload was. `size_budget` is the expanded bytes the upload has
    left across its remaining entries. Returns `(result, bytes_read)` so the
    caller can debit it.
    """
    # Sniff before spending disk, so a file that is not a mapping costs one
    # small read.
    with _corrupt_zip_is_a_bad_request():
        head = proguard_file.read(_PROGUARD_SNIFF_BYTES)
    metadata = extract_proguard_metadata(head)
    if metadata is None:
        return None, len(head)

    with tempfile.NamedTemporaryFile("br+") as tmp:
        # The checksum is known only once the whole entry is read, so the
        # temp file is written even when the blob turns out to exist already.
        checksum, size = await sync_to_async(_spool_entry)(
            proguard_file, head, tmp, size_budget
        )

        blob = await FileBlob.objects.filter(checksum=checksum).afirst()

        if blob is None:
            blob = FileBlob(checksum=checksum, size=size)
            await sync_to_async(blob.blob.save)(filename, DjangoFile(tmp))
            await blob.asave()

        fileobj = await File.objects.filter(checksum=checksum).afirst()

        if fileobj is None:
            fileobj = File()
            fileobj.name = filename
            fileobj.headers = {}
            fileobj.checksum = checksum
            fileobj.size = size
            fileobj.blob = blob
            await fileobj.asave()

        dif = await DebugInformationFile.objects.filter(
            file__checksum=checksum, project=project
        ).afirst()

        if dif is None:
            dif = DebugInformationFile()
            dif.name = filename
            dif.project = project
            dif.file = fileobj
            dif.data = {
                "arch": metadata["arch"],
                "debug_id": normalize_debug_id(proguard_id),
                "symbol_type": "proguard",
                "features": ["mapping"],
            }
            await dif.asave()

        result = {
            "id": dif.id,
            "debugId": proguard_id,
            "cpuName": "any",
            "objectName": "proguard-mapping",
            "symbolType": "proguard",
            "size": size,
            "sha1": checksum,
            "data": {"features": ["mapping"]},
            "headers": {"Content-Type": "text/x-proguard+plain"},
            "dateCreated": fileobj.created,
        }

        return result, size


@optional_slash(
    router,
    "get",
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/files/dsyms/",
    response=list[DebugFileSchema],
    by_alias=True,
)
@paginate
# Gates the project rather than the list, so an unknown project stays a 404.
@org_scoped(["project:read", "project:write", "project:admin"], Project)
async def list_dsyms(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    response: HttpResponse,
):
    project = await aget_object_or_404(qs, slug=project_slug.lower())

    return DebugInformationFile.objects.filter(project=project).select_related("file")


@optional_slash(
    router,
    "post",
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/files/dsyms/",
)
@org_scoped(["project:write", "project:admin", "project:releases"], Project)
async def dsyms(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    file: UploadedFile = NinjaFile(...),
):
    project = await aget_object_or_404(qs, slug=project_slug.lower())
    if file.size > MAX_UPLOAD_BLOB_SIZE:
        raise HttpError(
            400,
            "File size too large",
        )

    content = file.read()

    buffer = io.BytesIO(content)

    if zipfile.is_zipfile(buffer) is False:
        raise HttpError(400, "Invalid file type uploaded")

    results = []

    # One budget for the whole upload: every entry is stored, so a per-entry
    # cap would not bound a payload split over more entries.
    size_budget = settings.GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE
    try:
        _reject_oversized_input(
            buffer, size_budget, settings.GLITCHTIP_MAX_ARCHIVE_MEMBERS
        )
    except ZipExtractionLimitExceeded as err:
        raise HttpError(400, str(err)) from err

    with _corrupt_zip_is_a_bad_request():
        archive = zipfile.ZipFile(buffer)
    with archive as uploaded_zip_file:
        # The pre-open check bounds directory bytes, not the entry count, and
        # each entry costs a stored blob and several queries.
        if len(uploaded_zip_file.namelist()) > settings.GLITCHTIP_MAX_ARCHIVE_MEMBERS:
            raise HttpError(400, "Too many files in the uploaded archive")
        for filename in uploaded_zip_file.namelist():
            proguard_id = extract_proguard_id(filename)
            if proguard_id is None:
                raise HttpError(400, "")

            with _corrupt_zip_is_a_bad_request():
                entry = uploaded_zip_file.open(filename)
            with entry as proguard_file:
                result, size = await create_dif_from_read_only_file(
                    proguard_file, project, proguard_id, filename, size_budget
                )
                if result is None:
                    raise HttpError(
                        400,
                        "Invalid proguard mapping file uploaded",
                    )
                size_budget -= size
                results.append(result)

    return results
