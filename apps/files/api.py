"""Port of sentry.api.endpoints.chunk.ChunkUploadEndpoint"""

import asyncio
import logging
import zlib
from concurrent.futures import ThreadPoolExecutor
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
# Decompressed chunk bytes stream to disk past this point, bounding heap
# usage during decompression.
CHUNK_SPOOL_MAX_SIZE = 1024 * 1024
CHUNK_READ_SIZE = 64 * 1024
# gzip framing can slightly exceed the decompressed size for incompressible
# data, hence the slack over CHUNK_UPLOAD_BLOB_SIZE. Bounding the compressed
# input bounds decompression work: output-only caps never fire on gzip
# streams built from empty members, which cost CPU per member while
# producing zero bytes.
MAX_COMPRESSED_CHUNK_SIZE = CHUNK_UPLOAD_BLOB_SIZE + CHUNK_READ_SIZE

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


# Enforces the MAX_CONCURRENCY advertised to clients server-side, and keeps
# chunk decompression off the loop's default executor so an upload burst
# queues here instead of starving cold-storage reads (and vice versa).
_decompress_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENCY, thread_name_prefix="chunk-decompress"
)


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
    if upload.size and upload.size > MAX_COMPRESSED_CHUNK_SIZE:
        raise InvalidChunk("Chunk size too large")
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
        if digest.hexdigest() != upload.name:
            raise InvalidChunk("Chunk checksum mismatch")
    except (BadGzipFile, EOFError, zlib.error) as err:
        spooled.close()
        raise InvalidChunk("Invalid gzip chunk") from err
    except BaseException:
        # Close deterministically on rejection; a spool past the threshold
        # holds an open fd and disk space until garbage collection otherwise.
        spooled.close()
        raise
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
    """Upload one or more gzipped files to save"""
    logger = logging.getLogger("glitchtip.files")
    logger.info("chunkupload.start")

    # Membership check; the organization itself is not otherwise needed.
    await aget_object_or_404(
        Organization, slug=organization_slug.lower(), users=request.auth.user_id
    )

    logger.info("chunkupload.post.files", extra={"len": len(file_gzip)})

    if len(file_gzip) > MAX_CHUNKS_PER_REQUEST:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, "Too many chunks")

    try:
        # Decompression is streaming but CPU-bound; the dedicated executor
        # keeps the event loop free and lets zlib run on released-GIL time.
        files = await asyncio.get_running_loop().run_in_executor(
            _decompress_executor,
            lambda: [decompress_chunk(chunk) for chunk in file_gzip],
        )
    except InvalidChunk as err:
        logger.info("chunkupload.end", extra={"status": 400, "reason": str(err)})
        raise HttpError(400, str(err)) from err

    # Request size needs no separate check: each chunk is capped mid-inflate
    # at CHUNK_UPLOAD_BLOB_SIZE and the chunk count is capped above. If
    # MAX_CHUNKS_PER_REQUEST is ever raised above 1, move a request-wide
    # byte budget into decompress_chunk instead of summing after the fact.
    try:
        await FileBlob.from_files([(file, file.name) for file in files], logger=logger)
    except OSError as err:
        logger.info("chunkupload.end", extra={"status": 400, "reason": str(err)})
        raise HttpError(400, str(err)) from err
    finally:
        for file in files:
            file.close()

    logger.info("chunkupload.end", extra={"status": 200})
