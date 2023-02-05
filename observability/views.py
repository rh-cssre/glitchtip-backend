from django.urls import path
from django_prometheus import exports
from rest_framework.permissions import IsAdminUser
from rest_framework.views import APIView


class DjangoPrometheusMetrics(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        return exports.ExportToDjangoView(request)
