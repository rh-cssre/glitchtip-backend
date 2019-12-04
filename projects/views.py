from rest_framework import viewsets
from django.shortcuts import get_object_or_404
from .models import Project, ProjectKey
from .serializers import ProjectSerializer, ProjectKeySerializer


class ProjectViewSet(viewsets.ModelViewSet):
    """
    Detail view is under /api/0/projects/{organization_slug}/{project_slug}/

    Project keys/DSN's are available at /api/0/projects/{organization_slug}/{project_slug}/keys/
    """

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    lookup_value_regex = r"(?P<org_slug>[^/.]+)/(?P<slug>[-\w]+)"

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        obj = get_object_or_404(
            queryset,
            slug=self.kwargs["slug"],
            organization__slug=self.kwargs["org_slug"],
        )

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj


class ProjectKeyViewSet(viewsets.ModelViewSet):
    queryset = ProjectKey.objects.all()
    serializer_class = ProjectKeySerializer
    lookup_field = "public_key"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(
                project__slug=self.kwargs["slug"],
                project__organization__slug=self.kwargs["org_slug"],
            )
        )
