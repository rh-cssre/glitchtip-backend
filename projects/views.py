from rest_framework import viewsets, exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from teams.models import Team
from teams.views import NestedTeamViewSet
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
            team_slug = self.kwargs.get("team_slug")
            if team_slug:
                queryset = queryset.filter(team__slug=team_slug)
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

        new_project = serializer.save(organization=organization)
        if new_project:
            new_project.team_set.add(team)


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


class ProjectTeamViewSet(NestedTeamViewSet):
    @action(
        methods=["post", "delete"], detail=False, url_path=("(?P<team_slug>[-\w]+)")
    )
    def add_remove_project(
        self,
        request,
        project_pk=None,
        project_slug=None,
        organization_slug=None,
        team_slug=None,
    ):
        """ Add/remove team to a project """
        team = get_object_or_404(self.get_queryset(), slug=team_slug)
        project = get_object_or_404(
            Project,
            slug=project_slug,
            organization__slug=organization_slug,
            organization__users=self.request.user,
            organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
        )
        serializer = ProjectSerializer(instance=project, context={"request": request})
        if request.method == "POST":
            project.team_set.add(team)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        project.team_set.remove(team)
        return Response(serializer.data)
