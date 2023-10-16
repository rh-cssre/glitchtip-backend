from django.shortcuts import get_object_or_404
from rest_framework import exceptions, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from organizations_ext.models import Organization, OrganizationUserRole
from teams.models import Team
from teams.views import NestedTeamViewSet

from .models import Project, ProjectKey
from .permissions import ProjectKeyPermission, ProjectPermission
from .serializers.serializers import (
    BaseProjectSerializer,
    OrganizationProjectSerializer,
    ProjectDetailSerializer,
    ProjectKeySerializer,
    ProjectSerializer,
)


class BaseProjectViewSet(viewsets.ReadOnlyModelViewSet):
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
        ).prefetch_related("team_set")
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        team_slug = self.kwargs.get("team_slug")
        if team_slug:
            queryset = queryset.filter(team__slug=team_slug)
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

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return ProjectDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        queryset = super().get_queryset().select_related("organization")
        if self.action in ["retrieve"]:
            queryset = queryset.prefetch_related("team_set")
        return queryset


class TeamProjectViewSet(
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    BaseProjectViewSet,
):
    """
    Detail view is under /api/0/projects/{organization_slug}/{project_slug}/

    Project keys/DSN's are available at /api/0/projects/{organization_slug}/{project_slug}/keys/
    """

    serializer_class = ProjectDetailSerializer

    def perform_create(self, serializer):
        team = None
        if self.kwargs.get("team_slug"):
            try:
                team = Team.objects.get(
                    slug=self.kwargs.get("team_slug"),
                    organization__slug=self.kwargs.get("organization_slug"),
                    organization__users=self.request.user,
                    organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
                )
            except Team.DoesNotExist as err:
                raise exceptions.ValidationError("Team not found") from err
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"),
                users=self.request.user,
                organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
        except Organization.DoesNotExist as err:
            raise exceptions.ValidationError("Organization not found") from err

        new_project = serializer.save(organization=organization)
        if new_project and team:
            new_project.team_set.add(team)


class OrganizationProjectsViewSet(BaseProjectViewSet):
    """
    /organizations/<org-slug>/projects/

    Includes teams
    """

    serializer_class = OrganizationProjectSerializer

    def get_queryset(self, *args, **kwargs):
        queryset = super().get_queryset(*args, **kwargs)
        queries = self.request.GET.get("query")
        # Pretty simplistic filters that don't match how django-filter works
        # If this needs used more extensively, it should be abstracted more
        if queries:
            for query in queries.split():
                query_part = query.split(":", 1)
                if len(query_part) == 2:
                    query_name, query_value = query_part
                    if query_name == "team":
                        queryset = queryset.filter(team__slug=query_value)
                    if query_name == "!team":
                        queryset = queryset.exclude(team__slug=query_value)
        return queryset


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
        """Add/remove team to a project"""
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
