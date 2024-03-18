from django.db.models import Q
from rest_framework import exceptions, viewsets

from apps.organizations_ext.models import OrganizationUserRole
from apps.projects.models import Project

from .models import ProjectAlert
from .permissions import ProjectAlertPermission
from .serializers import ProjectAlertSerializer


class ProjectAlertViewSet(viewsets.ModelViewSet):
    queryset = ProjectAlert.objects.distinct()
    serializer_class = ProjectAlertSerializer
    permission_classes = [ProjectAlertPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        return self.queryset.filter(
            project__slug=self.kwargs.get("project_slug"),
            project__organization__users=self.request.user,
            project__organization__slug=self.kwargs.get("organization_slug"),
        )

    def perform_create(self, serializer):
        try:
            project = (
                Project.objects.distinct()
                .filter(
                    Q(
                        organization__users=self.request.user,
                        organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
                    )
                    | Q(team__members__user=self.request.user)
                )
                .get(
                    slug=self.kwargs.get("project_slug"),
                    organization__slug=self.kwargs.get("organization_slug"),
                )
            )
        except Project.DoesNotExist as err:
            raise exceptions.ValidationError("Project does not exist") from err
        serializer.save(project=project)
