from django_prometheus import exports
from rest_framework.permissions import IsAdminUser
from rest_framework.views import APIView

from organizations_ext.models import Organization

from .metrics import organizations_metric, projects_metric


class DjangoPrometheusMetrics(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        orgs = Organization.objects.prefetch_related("projects")
        for org in orgs.all():
            projects_metric.labels(org.slug).set(org.projects.count())
        organizations_metric.set(orgs.count())
        return exports.ExportToDjangoView(request)
