"""Port of sentry.api.endpoints.chunk.ChunkUploadEndpoint"""

import asyncio
import logging
import zlib
from gzip import BadGzipFile, GzipFile
from hashlib import sha1
from tempfile import SpooledTemporaryFile

from django.conf import settings
from django.core.files.base import File as FileObj
from django.shortcuts import aget_object_or_404
from django.urls import reverse
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import has_permission

from .models import FileBlob

CHUNK_UPLOAD_BLOB_SIZE = 32 * 1024 * 1024  # 32MB
MAX_CHUNKS_PER_REQUEST = 1
MAX_REQUEST_SIZE = CHUNK_UPLOAD_BLOB_SIZE
MAX_CONCURRENCY = 1
HASH_ALGORITHM = "sha1"
# Decompressed chunk bytes stream to disk past this point so a chunk never
# occupies more than this much process heap.
CHUNK_SPOOL_MAX_SIZE = 1024 * 1024
CHUNK_READ_SIZE = 64 * 1024

CHUNK_UPLOAD_ACCEPT = (
    "debug_files",  # DIF assemble
    "release_files",  # Release files assemble
    "pdbs",  # PDB upload and debug id override
    "sources",  # Source artifact bundle upload
    "artifact_bundles",  # Artifact bundles contain debug ids to link source to sourcemaps
    "proguard",
)


class InvalidChunk(Exception):
    """A chunk that must be rejected with a 400 rather than stored."""


def decompress_chunk(upload) -> FileObj:
    """
    Stream-decompress one gzipped chunk into a spooled temp file.

    The decompressed size is enforced while inflating, so a gzip bomb
    (deliberate or an accidentally huge file) aborts at the cap instead of
    expanding fully into memory. The SHA-1 of the decompressed content is
    computed in the same pass and verified against the client-supplied chunk
    name: that name becomes the FileBlob checksum, a global deduplication
    key, so accepting it unverified would let one organization poison the
    content another organization's assemble resolves to.
    """
    spooled = SpooledTemporaryFile(max_size=CHUNK_SPOOL_MAX_SIZE)
    digest = sha1()
    size = 0
    try:
        with GzipFile(fileobj=upload, mode="rb") as gz:
            while data := gz.read(CHUNK_READ_SIZE):
                size += len(data)
                if size > CHUNK_UPLOAD_BLOB_SIZE:
                    raise InvalidChunk("Chunk size too large")
                digest.update(data)
                spooled.write(data)
    except (BadGzipFile, EOFError, zlib.error) as err:
        raise InvalidChunk("Invalid gzip chunk") from err
    if digest.hexdigest() != upload.name:
        raise InvalidChunk("Chunk checksum mismatch")
    spooled.seek(0)
    file = FileObj(spooled, name=upload.name)
    file.size = size
    return file


router = Router()


@optional_slash(router, "get", "organizations/{slug:organization_slug}/chunk-upload/")
async def get_chunk_upload_info(request: AuthHttpRequest, organization_slug: str):
    """Get server settings for chunk file upload"""
    path = reverse("api:get_chunk_upload_info", args=[organization_slug])
    url = (
        path
        if settings.GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL
        else settings.GLITCHTIP_URL.geturl() + path
    )
    return {
        "url": url,
        "chunkSize": CHUNK_UPLOAD_BLOB_SIZE,
        "chunksPerRequest": MAX_CHUNKS_PER_REQUEST,
        "maxFileSize": 2147483648,
        "maxRequestSize": MAX_REQUEST_SIZE,
        "concurrency": MAX_CONCURRENCY,
        "hashAlgorithm": HASH_ALGORITHM,
        "compression": ["gzip"],
        "accept": CHUNK_UPLOAD_ACCEPT,
    }


@optional_slash(router, "post", "organizations/{slug:organization_slug}/chunk-upload/")
@has_permission(["project:write", "project:admin", "project:releases"])
async def chunk_upload(
    request: AuthHttpRequest,
    organization_slug: str,
    file_gzip: list[UploadedFile] = File(...),
):
    """Upload one more more gzipped files to save"""
    logger = logging.getLogger("glitchtip.files")
    logger.info("chunkupload.start")

    organization = await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )

    if len(file_gzip) == 0:
        # No files uploaded is ok
        logger.info("chunkupload.end", extra={"status": 200})
        return

    logger.info("chunkupload.post.files", extra={"len": len(file_gzip)})

    if len(file_gzip) > MAX_CHUNKS_PER_REQUEST:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, "Too many chunks")

    try:
        # Decompression is streaming but CPU-bound; a thread keeps the event
        # loop free and lets zlib run on released-GIL time.
        files = await asyncio.to_thread(
            lambda: [decompress_chunk(chunk) for chunk in file_gzip]
        )
    except InvalidChunk as err:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, str(err)) from err

    if sum(file.size for file in files) > MAX_REQUEST_SIZE:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, "Request too large")

    checksums = [file.name for file in files]

    try:
        await FileBlob.from_files(
            zip(files, checksums), organization=organization, logger=logger
        )
    except OSError as err:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, str(err)) from err

    logger.info("chunkupload.end", extra={"status": 200})
