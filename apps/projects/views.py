from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.filters import OrderingFilter

from .models import Project, ProjectKey
from .permissions import ProjectKeyPermission, ProjectPermission
from .serializers.serializers import (
    BaseProjectSerializer,
    OrganizationProjectSerializer,
    ProjectDetailSerializer,
    ProjectKeySerializer,
    ProjectSerializer,
)


class BaseProjectViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = BaseProjectSerializer
    queryset = Project.undeleted_objects.all()
    lookup_field = "slug"
    permission_classes = [ProjectPermission]

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        slug = self.kwargs.get("project_slug", self.kwargs.get("slug"))
        obj = get_object_or_404(
            queryset,
            slug=slug,
            organization__slug=self.kwargs["organization_slug"],
        )

        self.check_object_permissions(self.request, obj)

        return obj

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(
            organization__users=self.request.user
        ).prefetch_related("teams")
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        team_slug = self.kwargs.get("team_slug")
        if team_slug:
            queryset = queryset.filter(teams__slug=team_slug)
        return queryset


class ProjectViewSet(
    mixins.DestroyModelMixin, mixins.UpdateModelMixin, BaseProjectViewSet
):
    """
    /api/0/projects/

    Includes organization
    Detail view includes teams
    """

    serializer_class = ProjectSerializer
    filter_backends = [OrderingFilter]
    ordering = ["name"]
    ordering_fields = ["name"]
    lookup_field = "pk"
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<project_slug>[-\w]+)"


class TeamProjectViewSet(
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    BaseProjectViewSet,
):
    """
    Detail view is under /api/0/projects/{organization_slug}/{project_slug}/

    Project keys/DSN's are available at /api/0/projects/{organization_slug}/{project_slug}/keys/
    """

    serializer_class = ProjectDetailSerializer


class OrganizationProjectsViewSet(BaseProjectViewSet):
    """
    /organizations/<org-slug>/projects/

    Includes teams
    """

    serializer_class = OrganizationProjectSerializer


class ProjectKeyViewSet(viewsets.ModelViewSet):
    queryset = ProjectKey.objects.all()
    serializer_class = ProjectKeySerializer
    lookup_field = "public_key"
    permission_classes = [ProjectKeyPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        return (
            super()
            .get_queryset()
            .filter(
                project__slug=self.kwargs["project_slug"],
                project__organization__slug=self.kwargs["organization_slug"],
                project__organization__users=self.request.user,
            )
        )

    def perform_create(self, serializer):
        project = get_object_or_404(
            Project,
            slug=self.kwargs.get("project_slug"),
            organization__slug=self.kwargs["organization_slug"],
            organization__users=self.request.user,
        )
        serializer.save(project=project)
