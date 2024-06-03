from rest_framework import exceptions, mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization

from .models import Release, ReleaseFile
from .permissions import ReleaseFilePermission, ReleasePermission
from .serializers import (
    AssembleSerializer,
    ReleaseFileSerializer,
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


class ReleaseFileViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = ReleaseFile.objects.all()
    serializer_class = ReleaseFileSerializer
    permission_classes = [ReleaseFilePermission]

    def get_queryset(self):
        queryset = self.queryset
        if not self.request.user.is_authenticated:
            return queryset.none()

        queryset = queryset.filter(
            release__organization__users=self.request.user,
            release__organization__slug=self.kwargs.get("organization_slug"),
            release__version=self.kwargs.get("release_version"),
        )
        if self.kwargs.get("project_slug"):
            queryset = queryset.filter(
                release__projects__slug=self.kwargs.get("project_slug")
            )

        queryset = queryset.select_related("file")
        return queryset

    def perform_create(self, serializer):
        try:
            release = Release.objects.get(
                version=self.kwargs.get("release_version"),
                organization__slug=self.kwargs.get("organization_slug"),
            )
        except Release.DoesNotExist:
            raise exceptions.ValidationError("Release does not exist")

        serializer.save(release=release)
