from django.urls import path

from rest_framework.views import APIView
from rest_framework.permissions import IsAdminUser

from django_prometheus import exports


class DjangoPrometheusMetrics(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return exports.ExportToDjangoView(request)

