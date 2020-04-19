from rest_framework import viewsets, exceptions
from projects.models import Project
from .models import ProjectAlert
from .serializers import ProjectAlertSerializer


class ProjectAlertViewSet(viewsets.ModelViewSet):
    queryset = ProjectAlert.objects.all()
    serializer_class = ProjectAlertSerializer

    def get_queryset(self):
        return self.queryset.filter(
            project__slug=self.kwargs.get("project_slug"),
            project__organization__users=self.request.user,
            project__team__members=self.request.user,
            project__organization__slug=self.kwargs.get("organization_slug"),
        )

    def perform_create(self, serializer):
        try:
            project = Project.objects.get(
                slug=self.kwargs.get("project_slug"),
                organization__users=self.request.user,
                team__members=self.request.user,
                organization__slug=self.kwargs.get("organization_slug"),
            )
        except Project.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")
        serializer.save(project=project)
