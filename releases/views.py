from rest_framework import viewsets, exceptions, mixins
from rest_framework.response import Response
from rest_framework.decorators import action
from organizations_ext.models import Organization
from projects.models import Project
from files.tasks import assemble_artifacts_task
from .models import Release, ReleaseFile
from .serializers import (
    ReleaseSerializer,
    ReleaseUpdateSerializer,
    ReleaseFileSerializer,
    AssembleSerializer,
)
from .permissions import ReleasePermission, ReleaseFilePermission


class ReleaseViewSet(viewsets.ModelViewSet):
    queryset = Release.objects.all()
    serializer_class = ReleaseSerializer
    permission_classes = [ReleasePermission]
    lookup_field = "version"
    lookup_value_regex = "[^/]+"

    def get_serializer_class(self):
        serializer_class = self.serializer_class
        if self.request.method == "PUT":
            serializer_class = ReleaseUpdateSerializer
        return serializer_class

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        project_slug = self.kwargs.get("project_slug")
        if project_slug:
            queryset = queryset.filter(projects__slug=project_slug)
        return queryset

    def get_organization(self):
        try:
            return Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")

    def perform_create(self, serializer):
        organization = self.get_organization()
        try:
            project = Project.objects.get(
                slug=self.kwargs.get("project_slug"),
                organization=organization,
            )
        except Project.DoesNotExist:
            raise exceptions.ValidationError("Project does not exist")

        release = serializer.save(organization=organization)
        release.projects.add(project)

    @action(detail=True, methods=["post"])
    def assemble(self, request, organization_slug: str, version: str):
        organization = self.get_organization()
        release = self.get_object()
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
    mixins.ListModelMixin,
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
