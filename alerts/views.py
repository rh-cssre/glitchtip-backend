from rest_framework import viewsets, exceptions
from projects.models import Project
from .models import ProjectAlert
from .serializers import ProjectAlertSerializer
from .permissions import ProjectAlertPermission


class ProjectAlertViewSet(viewsets.ModelViewSet):
    queryset = ProjectAlert.objects.distinct()
    serializer_class = ProjectAlertSerializer
    permission_classes = [ProjectAlertPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        return self.queryset.filter(
            project__slug=self.kwargs.get("project_slug"),
            project__team__members__user=self.request.user,
            project__organization__slug=self.kwargs.get("organization_slug"),
        )

    def perform_create(self, serializer):
        try:
            project = Project.objects.distinct().get(
                slug=self.kwargs.get("project_slug"),
                team__members__user=self.request.user,
                organization__slug=self.kwargs.get("organization_slug"),
            )
        except Project.DoesNotExist:
            raise exceptions.ValidationError("Organization does not exist")
        serializer.save(project=project)
