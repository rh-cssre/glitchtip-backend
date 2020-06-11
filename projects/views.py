from rest_framework import viewsets, exceptions
from django.shortcuts import get_object_or_404
from teams.models import Team
from organizations_ext.models import Organization, OrganizationUserRole
from .models import Project, ProjectKey
from .serializers.serializers import ProjectSerializer, ProjectKeySerializer


class NestedProjectViewSet(viewsets.ModelViewSet):
    """
    Detail view is under /api/0/projects/{organization_slug}/{project_slug}/

    Project keys/DSN's are available at /api/0/projects/{organization_slug}/{project_slug}/keys/
    """

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        obj = get_object_or_404(
            queryset,
            slug=self.kwargs["project_slug"],
            organization__slug=self.kwargs["organization_slug"],
        )

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def get_queryset(self):
        if self.request.user.is_authenticated:
            queryset = self.queryset.filter(organization__users=self.request.user)
            organization_slug = self.kwargs.get("organization_slug")
            if organization_slug:
                queryset = queryset.filter(organization__slug=organization_slug)
            return queryset
        return self.queryset.none()

    def perform_create(self, serializer):
        try:
            team = Team.objects.get(
                slug=self.kwargs.get("team_slug"),
                organization__slug=self.kwargs.get("organization_slug"),
                organization__users=self.request.user,
                organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
        except Team.DoesNotExist:
            raise exceptions.ValidationError("Team not found")
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
                organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
        except Organization.DoesNotExist:
            raise exceptions.ValidationError("Organization not found")
        serializer.save(team=team, organization=organization)


class ProjectViewSet(NestedProjectViewSet):
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<project_slug>[-\w]+)"


class ProjectKeyViewSet(viewsets.ModelViewSet):
    queryset = ProjectKey.objects.all()
    serializer_class = ProjectKeySerializer
    lookup_field = "public_key"

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
