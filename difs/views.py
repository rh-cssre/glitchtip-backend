""" Port of sentry.api.endpoints.debug_files.DifAssembleEndpoint """
from django.shortcuts import get_object_or_404
from django.conf import settings
from django.db import transaction
from rest_framework.response import Response
from organizations_ext.models import Organization
from projects.models import Project
from rest_framework import views, exceptions, status
from .tasks import (
    difs_assemble, DIF_STATE_CREATED, DIF_STATE_OK,
    DIF_STATE_NOT_FOUND
)
from .models import DebugInformationFile
from files.models import FileBlob, File
from .permissions import (
    DifsAssemblePermission, ProjectReprocessingPermission,
    DymsPermission
)
from django.core.files import File as DjangoFile
import zipfile
import io
import re
import tempfile
from hashlib import sha1
from symbolic import ProguardMapper


MAX_UPLOAD_BLOB_SIZE = 8 * 1024 * 1024  # 8MB


def check_difs_enabled(func):
    def wrapper(*args, **kwargs):
        if settings.GLITCHTIP_ENABLE_DIFS is not True:
            raise exceptions.PermissionDenied()
        return func(*args, **kwargs)
    return wrapper


class DifsAssembleAPIView(views.APIView):
    permission_classes = [DifsAssemblePermission]

    @check_difs_enabled
    def post(self, request, organization_slug, project_slug):
        organization = get_object_or_404(
            Organization,
            slug=organization_slug.lower(),
            users=self.request.user
        )

        self.check_object_permissions(request, organization)

        project = get_object_or_404(
            Project, slug=project_slug.lower()
        )

        if project.organization.id != organization.id:
            raise exceptions.PermissionDenied(
                "The project is not under this organization")

        responses = {}

        files = request.data.items()

        for checksum, file in files:
            chunks = file.get("chunks", [])
            name = file.get("name", None)
            debug_id = file.get("debug_id", None)
            file = DebugInformationFile.objects.filter(
                project__slug=project_slug, file__checksum=checksum
            ).select_related("file").first()

            if file is not None:
                responses[checksum] = {
                    "state": DIF_STATE_OK,
                    "missingChunks": [],
                }
                continue

            existed_chunks = FileBlob.objects.filter(
                checksum__in=chunks
            ).values_list("checksum", flat=True)

            missing_chunks = list(set(chunks) - set(existed_chunks))

            if len(missing_chunks) != 0:
                responses[checksum] = {
                    "state": DIF_STATE_NOT_FOUND,
                    "missingChunks": missing_chunks
                }
                continue

            responses[checksum] = {
                "state": DIF_STATE_CREATED,
                "missingChunks": []
            }
            difs_assemble.delay(project_slug, name, checksum, chunks, debug_id)

        return Response(responses)


class ProjectReprocessingAPIView(views.APIView):
    """
    Not implemented. It is a dummy API to keep `sentry-cli upload-dif` happy
    """

    permission_classes = [ProjectReprocessingPermission]

    @check_difs_enabled
    def post(self, request, organization_slug, project_slug):
        return Response()


def extract_proguard_id(name):
    match = re.search('proguard/([-a-fA-F0-9]+).txt', name)
    if match is None:
        return
    return match.group(1)


def extract_proguard_metadata(proguard_file):
    try:
        mapper = ProguardMapper.open(proguard_file)

        if (mapper is None):
            return

        metadata = {
            "arch": "any",
            "feature": "mapping"
        }

        return metadata

    except Exception:
        pass


class DsymsAPIView(views.APIView):
    """
    Implementation of /files/dsyms API View
    """
    permission_classes = [DymsPermission]

    @check_difs_enabled
    def post(self, request, organization_slug, project_slug):
        organization = get_object_or_404(
            Organization,
            slug=organization_slug.lower(),
            users=self.request.user
        )

        self.check_object_permissions(request, organization)

        project = get_object_or_404(
            Project, slug=project_slug.lower()
        )

        if project.organization.id != organization.id:
            raise exceptions.PermissionDenied(
                "The project is not under this organization")

        if "file" not in request.data:
            return Response(
                {"error": "No file uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            file = request.data["file"]
            if file.size > MAX_UPLOAD_BLOB_SIZE:
                return Response(
                    {"error": "File size too large"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            content = file.read()

            buffer = io.BytesIO(content)

            if zipfile.is_zipfile(buffer) is False:
                return Response(
                    {"error": "Invalid file type uploaded"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            results = []

            with zipfile.ZipFile(buffer) as uploaded_zip_file:
                for filename in uploaded_zip_file.namelist():
                    proguard_id = extract_proguard_id(filename)
                    if proguard_id is None:
                        return Response(
                            {"error": "Invalid proguard mapping file uploaded"},  # noqa
                            status=status.HTTP_400_BAD_REQUEST,
                        )

                    with uploaded_zip_file.open(filename) as proguard_file:
                        result = self.create_dif_from_read_only_file(
                            proguard_file,
                            project,
                            proguard_id,
                            filename)
                        if result is None:
                            return Response(
                                {"error": "Invalid proguard mapping file uploaded"},  # noqa
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        results.append(result)

            return Response(results)

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def create_dif_from_read_only_file(
        self,
        proguard_file,
        project,
        proguard_id,
        filename
    ):
        with tempfile.NamedTemporaryFile("br+") as tmp:
            content = proguard_file.read()
            tmp.write(content)
            tmp.flush()
            metadata = extract_proguard_metadata(tmp.name)
            if metadata is None:
                return None
            checksum = sha1(content).hexdigest()
            with transaction.atomic():
                size = len(content)

                blob = FileBlob.objects.filter(
                    checksum=checksum
                ).first()

                if blob is None:
                    blob = FileBlob(
                        checksum=checksum,  # noqa
                        size=size
                    )
                    blob.blob.save(filename, DjangoFile(tmp))
                    blob.save()

                fileobj = File.objects.filter(
                    checksum=checksum
                ).first()

                if fileobj is None:
                    fileobj = File()
                    fileobj.name = filename
                    fileobj.headers = {}
                    fileobj.checksum = checksum
                    fileobj.size = size
                    fileobj.save()

                    fileobj.blobs.set([blob])

                dif = DebugInformationFile.objects.filter(
                    file__checksum=checksum,
                    project=project
                ).first()

                if dif is None:
                    dif = DebugInformationFile()
                    dif.name = filename
                    dif.project = project
                    dif.file = fileobj
                    dif.data = {
                        "arch": metadata["arch"],
                        "debug_id": proguard_id,
                        "symbol_type": "proguard",
                        "features": ["mapping"]
                    }
                    dif.save()

                result = {
                    "id": dif.id,
                    "debugId": proguard_id,
                    "cpuName": "any",
                    "objectName": "proguard-mapping",
                    "symbolType": "proguard",
                    "size": size,
                    "sha1": checksum,
                    "data": {
                        "features": ["mapping"]
                    },
                    "headers": {
                        "Content-Type": "text/x-proguard+plain"
                    },
                    "dateCreated": fileobj.created,
                }

                return result
