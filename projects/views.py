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
    lookup_value_regex = "([^/.]+)/(?P<proj_slug>[-\w]+)"
    lookup_field = "slug"

    def get_object(self):
        # TODO make generic solution
        org_slug = self.kwargs.get("slug").split("/")[0]
        queryset = self.filter_queryset(self.get_queryset())
        filter_kwargs = {
            self.lookup_field: self.kwargs["proj_slug"],
            "organization__slug": org_slug,
        }
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj


class ProjectKeyViewSet(viewsets.ModelViewSet):
    queryset = ProjectKey.objects.all()
    serializer_class = ProjectKeySerializer
    lookup_field = "slug"
