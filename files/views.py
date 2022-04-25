""" Port of sentry.api.endpoints.chunk.ChunkUploadEndpoint """
import logging
from io import BytesIO
from gzip import GzipFile
from django.conf import settings
from django.urls import reverse
from django.shortcuts import get_object_or_404
from rest_framework import views, status
from rest_framework.response import Response
from organizations_ext.models import Organization
from .models import FileBlob
from .permissions import ChunkUploadPermission

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
)


class GzipChunk(BytesIO):
    def __init__(self, file):
        data = GzipFile(fileobj=file, mode="rb").read()
        self.size = len(data)
        self.name = file.name
        super().__init__(data)


class ChunkUploadAPIView(views.APIView):
    permission_classes = [ChunkUploadPermission]

    def get(self, request, organization_slug):
        url = settings.GLITCHTIP_URL.geturl() + reverse(
            "chunk-upload", args=[organization_slug]
        )
        return Response(
            {
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
        )

    def post(self, request, organization_slug):
        logger = logging.getLogger("glitchtip.files")
        logger.info("chunkupload.start")

        organization = get_object_or_404(
            Organization, slug=organization_slug.lower(), users=self.request.user
        )
        self.check_object_permissions(request, organization)

        files = request.data.getlist("file")
        files += [GzipChunk(chunk) for chunk in request.data.getlist("file_gzip")]

        if len(files) == 0:
            # No files uploaded is ok
            logger.info("chunkupload.end", extra={"status": status.HTTP_200_OK})
            return Response(status=status.HTTP_200_OK)

        logger.info("chunkupload.post.files", extra={"len": len(files)})

        # Validate file size
        checksums = []
        size = 0
        for chunk in files:
            size += chunk.size
            if chunk.size > CHUNK_UPLOAD_BLOB_SIZE:
                logger.info(
                    "chunkupload.end", extra={"status": status.HTTP_400_BAD_REQUEST}
                )
                return Response(
                    {"error": "Chunk size too large"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            checksums.append(chunk.name)

        if size > MAX_REQUEST_SIZE:
            logger.info(
                "chunkupload.end", extra={"status": status.HTTP_400_BAD_REQUEST}
            )
            return Response(
                {"error": "Request too large"}, status=status.HTTP_400_BAD_REQUEST
            )

        if len(files) > MAX_CHUNKS_PER_REQUEST:
            logger.info(
                "chunkupload.end", extra={"status": status.HTTP_400_BAD_REQUEST}
            )
            return Response(
                {"error": "Too many chunks"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            FileBlob.from_files(
                zip(files, checksums), organization=organization, logger=logger
            )
        except IOError as err:
            logger.info(
                "chunkupload.end", extra={"status": status.HTTP_400_BAD_REQUEST}
            )
            return Response({"error": str(err)}, status=status.HTTP_400_BAD_REQUEST)

        logger.info("chunkupload.end", extra={"status": status.HTTP_200_OK})
        return Response(status=status.HTTP_200_OK)
