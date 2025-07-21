"""Port of sentry.api.endpoints.chunk.ChunkUploadEndpoint"""

import logging
from gzip import GzipFile
from io import BytesIO

from django.conf import settings
from django.shortcuts import aget_object_or_404
from django.urls import reverse
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .models import FileBlob

# Force just one blob
CHUNK_UPLOAD_BLOB_SIZE = 32 * 1024 * 1024  # 32MB
MAX_CHUNKS_PER_REQUEST = 1
MAX_REQUEST_SIZE = CHUNK_UPLOAD_BLOB_SIZE
MAX_CONCURRENCY = 1
HASH_ALGORITHM = "sha1"

CHUNK_UPLOAD_ACCEPT = (
    "debug_files",  # DIF assemble
    "release_files",  # Release files assemble
    "pdbs",  # PDB upload and debug id override
    "sources",  # Source artifact bundle upload
    "artifact_bundles",  # Artifact bundles contain debug ids to link source to sourcemaps
)


class GzipChunk(BytesIO):
    def __init__(self, file):
        data = GzipFile(fileobj=file, mode="rb").read()
        self.size = len(data)
        self.name = file.name
        super().__init__(data)


router = Router()


@router.get("organizations/{slug:organization_slug}/chunk-upload/")
async def get_chunk_upload_info(request: AuthHttpRequest, organization_slug: str):
    """Get server settings for chunk file upload"""
    url = settings.GLITCHTIP_URL.geturl() + reverse(
        "api:get_chunk_upload_info", args=[organization_slug]
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


@router.post("organizations/{slug:organization_slug}/chunk-upload/")
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

    files = [GzipChunk(chunk) for chunk in file_gzip]

    if len(files) == 0:
        # No files uploaded is ok
        logger.info("chunkupload.end", extra={"status": 200})
        return

    logger.info("chunkupload.post.files", extra={"len": len(files)})

    # Validate file size
    checksums = []
    size = 0
    for chunk in files:
        size += chunk.size
        if chunk.size > CHUNK_UPLOAD_BLOB_SIZE:
            logger.info("chunkupload.end", extra={"status": 400})
            raise HttpError(400, "Chunk size too large")
        checksums.append(chunk.name)

    if size > MAX_REQUEST_SIZE:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, "Request too large")

    if len(files) > MAX_CHUNKS_PER_REQUEST:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, "Too many chunks")

    try:
        await FileBlob.from_files(
            zip(files, checksums), organization=organization, logger=logger
        )
    except IOError as err:
        logger.info("chunkupload.end", extra={"status": 400})
        raise HttpError(400, str(err)) from err

    logger.info("chunkupload.end", extra={"status": 200})
