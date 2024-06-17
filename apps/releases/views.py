from rest_framework import exceptions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization

from .models import Release
from .permissions import ReleasePermission
from .serializers import (
    AssembleSerializer,
    ReleaseSerializer,
)


class ReleaseViewSet(viewsets.ModelViewSet):
    """
    /organizations/<org-slug>/releases/

    Sentry includes only project name and slug in nested list. This view uses ProjectReferenceSerializer,
    which also includes id and platform, for consistency.
    """

    queryset = Release.objects.all()
    serializer_class = ReleaseSerializer
    permission_classes = [ReleasePermission]
    lookup_field = "version"
    lookup_value_regex = "[^/]+"

    def get_organization(self):
        try:
            return Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")

    @action(detail=True, methods=["post"])
    def assemble(self, request, organization_slug: str, version: str):
        organization = self.get_organization()
        # release = self.get_object()
        serializer = AssembleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        checksum = serializer.validated_data.get("checksum", None)
        chunks = serializer.validated_data.get("chunks", [])

        assemble_artifacts_task.delay(
            org_id=organization.id,
            version=version,
            checksum=checksum,
            chunks=chunks,
        )

        # TODO should return more state's
        return Response({"state": "ok", "missingChunks": []})
