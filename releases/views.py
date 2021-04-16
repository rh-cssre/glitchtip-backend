from rest_framework import viewsets, exceptions
from rest_framework.response import Response
from rest_framework.decorators import action
from organizations_ext.models import Organization
from projects.models import Project
from .models import Release
from .serializers import ReleaseSerializer, AssembleSerializer
from .permissions import ReleasePermission


class ReleaseViewSet(viewsets.ModelViewSet):
    queryset = Release.objects.all()
    serializer_class = ReleaseSerializer
    permission_classes = [ReleasePermission]
    lookup_field = "version"
    lookup_value_regex = "[^/]+"

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"), users=self.request.user,
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")
        try:
            project = Project.objects.get(
                slug=self.kwargs.get("project_slug"), organization=organization,
            )
        except Project.DoesNotExist:
            raise exceptions.ValidationError("Project does not exist")

        release = serializer.save(organization=organization)
        release.projects.add(project)

    @action(detail=True, methods=["post"])
    def assemble(self, request, organization_slug: str, version: str):
        release = self.get_object()
        serializer = AssembleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        print(release)
        print(serializer.validated_data)
        return Response({"state": "ok", "missingChunks": []})
