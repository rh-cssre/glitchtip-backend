from rest_framework import viewsets

from .models import Project
from .serializers.serializers import BaseProjectSerializer, ProjectSerializer


class BaseProjectViewSet(viewsets.GenericViewSet):
    serializer_class = BaseProjectSerializer
    queryset = Project.undeleted_objects.none()
    lookup_field = "slug"


class ProjectViewSet(BaseProjectViewSet):
    """
    /api/0/projects/

    Includes organization
    Detail view includes teams
    """

    serializer_class = ProjectSerializer
    lookup_field = "pk"
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<project_slug>[-\w]+)"
