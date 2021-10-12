""" Port of sentry.api.endpoints.debug_files.DifAssembleEndpoint """
from django.shortcuts import get_object_or_404
from django.conf import settings
from rest_framework.response import Response
from organizations_ext.models import Organization
from projects.models import Project
from rest_framework import views, exceptions
from .tasks import (
    difs_assemble, DIF_STATE_CREATED, DIF_STATE_OK,
    DIF_STATE_NOT_FOUND
)
from .models import DebugInformationFile
from files.models import FileBlob
from .permissions import (
    DifsAssemblePermission, ProjectReprocessingPermission,
)


class DifsAssembleAPIView(views.APIView):
    permission_classes = [DifsAssemblePermission]

    def post(self, request, organization_slug, project_slug):
        if settings.GLITCHTIP_ENABLE_DIFS != True:
            raise exceptions.PermissionDenied()

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
    Non implemented. It is a dummy API to keep `sentry-cli upload-dif` happy
    """

    permission_classes = [ProjectReprocessingPermission]

    def post(self, request, organization_slug, project_slug):
        if settings.GLITCHTIP_ENABLE_DIFS != True:
            raise exceptions.PermissionDenied()
        return Response()
