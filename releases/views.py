from rest_framework import viewsets, exceptions
from organizations_ext.models import Organization
from projects.models import Project
from .models import Release
from .serializers import ReleaseSerializer
from .permissions import ReleasePermission


class ReleaseViewSet(viewsets.ModelViewSet):
    queryset = Release.objects.all()
    serializer_class = ReleaseSerializer
    permission_classes = [ReleasePermission]
    lookup_field = "version"
    lookup_value_regex = "[^/]+"

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
