from django_prometheus import exports
from rest_framework.permissions import IsAdminUser
from rest_framework.views import APIView

from .metrics import compile_metrics


class DjangoPrometheusMetrics(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        compile_metrics()
        return exports.ExportToDjangoView(request)
