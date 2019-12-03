from rest_framework import viewsets
from rest_framework.decorators import action
from .models import Project, ProjectKey
from .serializers import ProjectSerializer, ProjectKeySerializer


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    lookup_fields = ["organization__slug", "slug"]


class ProjectKeyViewSet(viewsets.ModelViewSet):
    queryset = ProjectKey.objects.all()
    serializer_class = ProjectKeySerializer
    lookup_field = "slug"
